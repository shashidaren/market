#!/bin/bash
#
# /opt/market/indices_signal/run_pipeline.sh
#
# Indices signal pipeline
#
# Flow:
#
# price_collector.py
#        ↓
# prices.db
#        ↓
# telegram_bot.py   (runs signal_engine internally)
#        ↓
# outcome_tracker.py
#
# A 4H strategy does not need sub-minute cadence. Default cron is
# every 30 minutes; the collector skip-if-fresh means Yahoo is only
# hit when a new 4H / daily bar may have closed.
#

set -uo pipefail
# NOTE: -e intentionally omitted so a failing stage does not abort
#       later ones (outcome_tracker can still resolve from stored bars
#       even if Telegram is down).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOCKFILE="/tmp/indices_signal_pipeline.lock"

LOGFILE="/var/log/webscrap-indices-pipeline.log"

PYTHON="/usr/bin/python3"


log() {
    local level="$1"; shift
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [$level] $*" >> "$LOGFILE"
}


run_stage() {
    local name="$1"
    local script="$2"
    local logfile="$3"

    log "INFO" "Stage START: $name"
    "$PYTHON" "$SCRIPT_DIR/$script" >> "$logfile" 2>&1
    local rc=$?
    if [[ $rc -eq 0 ]]; then
        log "INFO" "Stage OK:    $name"
    else
        log "ERROR" "Stage FAIL:  $name exit=$rc — check $logfile"
    fi
    return $rc
}


# ============================================================
# ENVIRONMENT
# ============================================================

if [[ -f "$SCRIPT_DIR/.env" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    export TELEGRAM_BOT_TOKEN
    export TELEGRAM_CHAT_ID
fi


# ============================================================
# LOCK
# ============================================================

exec 200>"$LOCKFILE"

if ! flock -n 200; then
    log "WARN" "Previous pipeline still running, skipping."
    exit 0
fi


cd "$SCRIPT_DIR" || {
    log "ERROR" "Failed to cd to $SCRIPT_DIR"
    exit 1
}

log "INFO" "Starting indices pipeline."

run_stage "price_collector" "price_collector.py" "/var/log/webscrap-indices-collector.log"
# Engine + Telegram still run on stored bars if the collector
# skipped Yahoo (circuit open / skip-if-fresh).
run_stage "telegram_bot"    "telegram_bot.py"    "/var/log/webscrap-indices-telegram.log"
run_stage "outcome_tracker" "outcome_tracker.py" "/var/log/webscrap-indices-outcome.log"

log "INFO" "Indices pipeline completed."
