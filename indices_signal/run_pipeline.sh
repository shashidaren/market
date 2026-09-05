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
# telegram_bot.py
#
# Credentials loaded in order:
#   1. Environment already set
#   2. /opt/market/.env            (single source of truth)
#   3. /opt/market/indices_signal/.env  (migration fallback)
#
# Crontab example:
#   */30 * * * * BASH_ENV=/opt/market/.env /opt/market/indices_signal/run_pipeline.sh
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

LOCKFILE="/tmp/indices_signal_pipeline.lock"

LOGFILE="/var/log/webscrap-indices-pipeline.log"


# ============================================================
# ENVIRONMENT — root first, module-local fallback
# ============================================================

if [[ -f "$REPO_ROOT/.env" ]]; then
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
elif [[ -f "$SCRIPT_DIR/.env" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
fi

export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
export TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"


# ============================================================
# LOCK
# ============================================================

exec 200>"$LOCKFILE"

if ! flock -n 200; then

    echo \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ) [WARN] Previous pipeline still running, skipping." \
        >> "$LOGFILE"

    exit 0

fi


# ============================================================
# MOVE TO SCRIPT DIRECTORY
# ============================================================

cd "$SCRIPT_DIR"


echo \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ) [INFO] Starting indices pipeline." \
    >> "$LOGFILE"


# ============================================================
# COLLECT PRICES
# ============================================================

/usr/bin/python3 \
    price_collector.py \
    >> /var/log/webscrap-indices-collector.log \
    2>&1


# ============================================================
# ANALYZE AND SEND SIGNALS
# ============================================================

/usr/bin/python3 \
    telegram_bot.py \
    >> /var/log/webscrap-indices-telegram.log \
    2>&1


# ============================================================
# RESOLVE OUTCOMES (TP / SL hit)
# ============================================================

/usr/bin/python3 \
    outcome_tracker.py \
    >> /var/log/webscrap-indices-outcome.log \
    2>&1


echo \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ) [INFO] Indices pipeline completed." \
    >> "$LOGFILE"
