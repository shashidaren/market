#!/bin/bash
#
# /opt/market/indices_signal/install_cron.sh
#
# Installs (or re-enables) the indices signal pipeline cron job.
#
# Safe to re-run: it removes any existing indices-signal-pipeline block
# (commented or not) and writes a fresh, active one — so this is also the
# command to use when the cron line was manually commented out.
#
# Usage:
#   cd /opt/market/indices_signal
#   ./install_cron.sh                      # every 30 minutes (default)
#   INTERVAL_MINUTES=15 ./install_cron.sh  # custom interval
#   ./install_cron.sh --dry-run            # show what would be written

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER="# --- indices-signal-pipeline ---"
END_MARKER="# --- end indices-signal-pipeline ---"

INTERVAL_MINUTES="${INTERVAL_MINUTES:-30}"
DRY_RUN=0

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

if (( INTERVAL_MINUTES < 1 )); then
    echo "ERROR: INTERVAL_MINUTES must be >= 1" >&2
    exit 1
fi

# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------

if [[ ! -x "$SCRIPT_DIR/run_pipeline.sh" ]]; then
    echo "ERROR: run_pipeline.sh not found/executable in $SCRIPT_DIR" >&2
    exit 1
fi

if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    echo "WARNING: $SCRIPT_DIR/.env is missing." >&2
    echo "         The cron will run but Telegram alerts will be skipped." >&2
    echo "         Copy .env.example to .env and fill in the tokens first." >&2
fi

# Quick Python dependency check
if ! python3 -c "import yfinance, pandas, requests" 2>/dev/null; then
    echo "WARNING: Missing Python deps (yfinance, pandas, requests)." >&2
    echo "         Run: pip install yfinance pandas requests --break-system-packages" >&2
fi

# ------------------------------------------------------------------
# Ensure log files exist (the pipeline appends to these)
# ------------------------------------------------------------------

for logfile in \
    /var/log/webscrap-indices-pipeline.log \
    /var/log/webscrap-indices-collector.log \
    /var/log/webscrap-indices-telegram.log \
    /var/log/webscrap-indices-outcome.log; do
    if [[ ! -f "$logfile" ]]; then
        if touch "$logfile" 2>/dev/null; then
            chmod 644 "$logfile"
        else
            echo "WARNING: Cannot create $logfile — cron may fail to write logs." >&2
            echo "         Fix with: sudo touch $logfile && sudo chmod 666 $logfile" >&2
        fi
    fi
done

# ------------------------------------------------------------------
# Build cron block
# ------------------------------------------------------------------

CRON_CMD="*/${INTERVAL_MINUTES} * * * * BASH_ENV=${SCRIPT_DIR}/.env ${SCRIPT_DIR}/run_pipeline.sh"

# Remove any existing block, then append the fresh (active) one.
merged_crontab="$(
    crontab -l 2>/dev/null \
        | sed "/${MARKER//\//\\/}/,/${END_MARKER//\//\\/}/d"

    echo
    echo "$MARKER"
    echo "# Installed by install_cron.sh — every ${INTERVAL_MINUTES} minute(s)"
    echo "$CRON_CMD"
    echo "$END_MARKER"
)"

if [[ "$DRY_RUN" == "1" ]]; then
    echo "=== DRY RUN — crontab would contain this block ==="
    printf '%s\n' "$merged_crontab" | grep -A4 "$MARKER"
    echo "================================================="
    exit 0
fi

printf '%s\n' "$merged_crontab" | crontab -

echo "✅ Indices signal cron installed — every ${INTERVAL_MINUTES} minute(s)"
echo
crontab -l | sed -n "/${MARKER//\//\\/}/,/${END_MARKER//\//\\/}/p"
