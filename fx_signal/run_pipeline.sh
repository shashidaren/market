#!/bin/bash
# =============================================================================
# FX signal pipeline — collector → engine → outcome_tracker → bot
#
# Chains all stages sequentially with flock to prevent overlapping runs.
# Each stage is run independently — a failure in one stage is logged but
# does NOT abort later stages (bot still delivers previously stored signals
# even if the engine produced nothing new this cycle).
#
# Crontab (preferred):
#   */5 * * * * BASH_ENV=/opt/market/.env /opt/market/fx_signal/run_pipeline.sh
#
# Credentials loaded in order:
#   1. Environment variables already set in cron/shell
#   2. /opt/market/.env          (single source of truth)
#   3. /opt/market/fx_signal/.env (migration fallback only)
#
# Required env vars:
#   TELEGRAM_BOT_TOKEN   — Telegram bot token
#   TELEGRAM_CHAT_ID     — Telegram chat/channel ID
#
# Optional env vars:
#   SIGNAL_MODE          — strict (default) | relaxed
#   FINNHUB_API_KEY      — enables economic calendar in Telegram messages
# =============================================================================

set -uo pipefail
# NOTE: -e intentionally omitted so a failing stage does not abort
#       subsequent stages. Each stage captures its own exit code below.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCKFILE="/tmp/fx_signal_pipeline.lock"
PIPELINE_LOG="/var/log/webscrap-fx-pipeline.log"
PYTHON="/usr/bin/python3"

# ------------------------------------------------------------------
# Logging helper
# ------------------------------------------------------------------
log() {
    local level="$1"; shift
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [$level] $*" >> "$PIPELINE_LOG"
}

# ------------------------------------------------------------------
# Run one stage — captures timing + exit code, never aborts pipeline
# ------------------------------------------------------------------
run_stage() {
    local name="$1"
    local script="$2"
    local logfile="$3"

    local start_ts
    start_ts=$(date +%s)

    log "INFO" "Stage START: $name"

    # Run the stage; capture exit code without triggering pipefail exit
    set +e
    "$PYTHON" "$SCRIPT_DIR/$script" >> "$logfile" 2>&1
    local exit_code=$?
    set -e

    local end_ts
    end_ts=$(date +%s)
    local elapsed=$(( end_ts - start_ts ))

    if [[ $exit_code -eq 0 ]]; then
        log "INFO" "Stage OK:    $name (${elapsed}s)"
    else
        log "ERROR" "Stage FAIL:  $name exit=$exit_code (${elapsed}s) — check $logfile"
    fi

    return $exit_code
}

# ------------------------------------------------------------------
# Load .env — root first (canonical), then module-local fallback
# ------------------------------------------------------------------
if [[ -f "$REPO_ROOT/.env" ]]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/.env"
elif [[ -f "$SCRIPT_DIR/.env" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/.env"
fi

# ------------------------------------------------------------------
# Market hours guard
# FX is closed:
#   - Saturday all day  (dow=6)
#   - Sunday before ~21:00 UTC (dow=7, hour < 21)
# ------------------------------------------------------------------
dow=$(date +%u)      # 1=Mon … 7=Sun
hour_utc=$(date -u +%H)   # 00-23

if [[ "$dow" -eq 6 ]]; then
    log "INFO" "Saturday — FX closed, skipping pipeline."
    exit 0
fi

if [[ "$dow" -eq 7 && "$hour_utc" -lt 21 ]]; then
    log "INFO" "Sunday before 21:00 UTC (currently ${hour_utc}:xx) — FX not yet open, skipping."
    exit 0
fi

# ------------------------------------------------------------------
# Prevent overlapping runs
# ------------------------------------------------------------------
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    log "WARN" "Previous run still in progress — skipping this cycle."
    exit 0
fi

# ------------------------------------------------------------------
# Validate required credentials before doing any work
# ------------------------------------------------------------------
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    log "ERROR" "TELEGRAM_BOT_TOKEN not set — bot will fail. Continuing anyway (signal storage still works)."
fi

if [[ -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    log "ERROR" "TELEGRAM_CHAT_ID not set — bot will fail. Continuing anyway."
fi

if [[ -z "${FINNHUB_API_KEY:-}" ]]; then
    log "INFO" "FINNHUB_API_KEY not set — calendar checks disabled in bot."
fi

# Export so child python processes inherit them
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
export TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
export FINNHUB_API_KEY="${FINNHUB_API_KEY:-}"
export SIGNAL_MODE="${SIGNAL_MODE:-strict}"

# ------------------------------------------------------------------
# Pipeline
# ------------------------------------------------------------------
PIPELINE_START=$(date +%s)
log "INFO" "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "INFO" "Pipeline starting — mode=$SIGNAL_MODE"

cd "$SCRIPT_DIR" || {
    log "ERROR" "Failed to cd to $SCRIPT_DIR"
    exit 1
}

# Stage 1 — Collect prices and store completed bars into prices.db
run_stage "price_collector"  "price_collector.py"  "/var/log/webscrap-fx-collector.log"
COLLECTOR_RC=$?

# Stage 2 — Evaluate signals on closed bars (only runs if collector succeeded)
if [[ $COLLECTOR_RC -eq 0 ]]; then
    run_stage "signal_engine"    "signal_engine.py"    "/var/log/webscrap-fx-signal.log"
else
    log "WARN" "Stage SKIP: signal_engine — collector failed, no fresh data"
fi

# Stage 3 — Resolve open trade outcomes against current price
#           (runs regardless of signal_engine — checks historical signals)
run_stage "outcome_tracker"  "outcome_tracker.py"  "/var/log/webscrap-fx-outcome.log"

# Stage 4 — Deliver undelivered signals to Telegram
#           (runs regardless — may deliver signals from a previous cycle)
run_stage "telegram_bot"     "telegram_bot.py"     "/var/log/webscrap-fx-telegram.log"

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
PIPELINE_END=$(date +%s)
PIPELINE_ELAPSED=$(( PIPELINE_END - PIPELINE_START ))
log "INFO" "Pipeline complete — total ${PIPELINE_ELAPSED}s"
log "INFO" "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
