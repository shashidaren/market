#!/usr/bin/env bash
#
# Installs the FX signal pipeline cron job.
#
# Two scheduling modes:
#   --pipeline (default)  : installs run_pipeline.sh (recommended)
#   --separate            : installs 3 staggered cron jobs (legacy)
#
# Safe to re-run: detects a marker comment and skips re-adding.
# Pass FORCE=1 to replace an existing block.
#
# Usage:
#   cd /opt/market/fx_signal
#   chmod +x install_cron.sh
#   TELEGRAM_BOT_TOKEN="123:abc" TELEGRAM_CHAT_ID="***REMOVED***" ./install_cron.sh
#
#   # custom interval (minutes), default 15
#   INTERVAL_MINUTES=5 FORCE=1 ./install_cron.sh
#
#   # legacy 3-job mode
#   TELEGRAM_BOT_TOKEN="123:abc" TELEGRAM_CHAT_ID="***REMOVED***" ./install_cron.sh --separate

set -euo pipefail

MARKER="# --- fx-signal-pipeline (managed by install_cron.sh) ---"
END_MARKER="# --- end fx-signal-pipeline ---"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$(command -v python3)"
INTERVAL_MINUTES="${INTERVAL_MINUTES:-15}"
FORCE="${FORCE:-0}"
MODE="pipeline"  # default

# Parse args
for arg in "$@"; do
    case "$arg" in
        --separate) MODE="separate" ;;
        --pipeline) MODE="pipeline" ;;
        --dry-run) DRY_RUN=1 ;;
    esac
done
DRY_RUN="${DRY_RUN:-0}"

ENV_FILE="$SCRIPT_DIR/.env"

# Load existing .env as defaults (so re-runs don't need the token passed
# again) — values already set in the environment take precedence.
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck source=/dev/null
    . "$ENV_FILE"
    set +a
fi

# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set." >&2
    echo "Example:" >&2
    echo '  TELEGRAM_BOT_TOKEN="123:abc" TELEGRAM_CHAT_ID="***REMOVED***" ./install_cron.sh' >&2
    exit 1
fi

if (( INTERVAL_MINUTES < 1 )); then
    echo "ERROR: INTERVAL_MINUTES must be >= 1" >&2
    exit 1
fi

if [[ "$MODE" == "pipeline" && ! -x "$SCRIPT_DIR/run_pipeline.sh" ]]; then
    echo "ERROR: run_pipeline.sh not found or not executable in $SCRIPT_DIR" >&2
    exit 1
fi

if [[ "$MODE" == "separate" ]]; then
    for f in price_collector.py signal_engine.py telegram_bot.py; do
        if [[ ! -f "$SCRIPT_DIR/$f" ]]; then
            echo "ERROR: $f not found in $SCRIPT_DIR" >&2
            exit 1
        fi
    done
fi

# Quick Python dependency check
if ! "$PYTHON_BIN" -c "import yfinance, pandas, requests" 2>/dev/null; then
    echo "WARNING: One or more Python dependencies (yfinance, pandas, requests) not found." >&2
    echo "         Run: pip install yfinance pandas requests --break-system-packages" >&2
fi

# ------------------------------------------------------------------
# Write secrets to .env (600 perms) — never embed in crontab.
# Preserves any existing entries (FINNHUB_API_KEY, SIGNAL_MODE, ...)
# instead of clobbering the whole file.
# ------------------------------------------------------------------

touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Drop the two keys we manage, then re-append them (safe against values
# containing $ or backticks — printf, not an unquoted heredoc).
grep -v -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=' "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || true
printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' \
    "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID" >> "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "Secrets updated in $ENV_FILE (mode 600, other keys preserved)"

# ------------------------------------------------------------------
# Ensure log files exist and are writable
# ------------------------------------------------------------------

for logfile in /var/log/webscrap-fx-collector.log /var/log/webscrap-fx-signal.log /var/log/webscrap-fx-telegram.log /var/log/webscrap-fx-pipeline.log; do
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

if [[ "$MODE" == "pipeline" ]]; then
    new_block=$(cat <<EOF

$MARKER
# Mode: single pipeline (run_pipeline.sh), every ${INTERVAL_MINUTES} min
# BASH_ENV sources $ENV_FILE for TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
*/$INTERVAL_MINUTES * * * * BASH_ENV=$ENV_FILE $SCRIPT_DIR/run_pipeline.sh
$END_MARKER
EOF
)
else
    # Legacy 3-job staggered mode
    new_block=$(cat <<EOF

$MARKER
# Mode: separate stages (legacy)
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
*/$INTERVAL_MINUTES * * * * cd $SCRIPT_DIR && $PYTHON_BIN price_collector.py >> /var/log/webscrap-fx-collector.log 2>&1
1-59/$INTERVAL_MINUTES * * * * cd $SCRIPT_DIR && SIGNAL_MODE=relaxed $PYTHON_BIN signal_engine.py >> /var/log/webscrap-fx-signal.log 2>&1
2-59/$INTERVAL_MINUTES * * * * cd $SCRIPT_DIR && $PYTHON_BIN telegram_bot.py >> /var/log/webscrap-fx-telegram.log 2>&1
$END_MARKER
EOF
)
fi

# ------------------------------------------------------------------
# Merge with existing crontab
# ------------------------------------------------------------------

existing_crontab="$(crontab -l 2>/dev/null || true)"

# Remove old block if present (using index() for robustness)
cleaned_crontab="$(awk -v m="$MARKER" -v e="$END_MARKER" '
    index($0, m) {skip=1}
    skip == 0 {print}
    index($0, e) {skip=0}
' <<< "$existing_crontab")"

# If marker was found but FORCE=0, skip
if [[ "$existing_crontab" != "$cleaned_crontab" && "$FORCE" != "1" ]]; then
    echo "FX signal cron block already present — nothing to do."
    echo "(Re-run with FORCE=1 to replace, or INTERVAL_MINUTES=N to change interval.)"
    exit 0
fi

merged_crontab="${cleaned_crontab}"$'\n'"${new_block}"

if [[ "$DRY_RUN" == "1" ]]; then
    echo "=== DRY RUN — crontab would look like this ==="
    printf '%s\n' "$merged_crontab"
    echo "=============================================="
    exit 0
fi

printf '%s\n' "$merged_crontab" | crontab -

echo "Cron jobs installed:"
crontab -l | grep -A4 "$MARKER"
echo

# ------------------------------------------------------------------
# Cron service check (with fallback for non-systemd)
# ------------------------------------------------------------------

if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-enabled cron >/dev/null 2>&1 || systemctl is-enabled crond >/dev/null 2>&1; then
        echo "cron service is enabled at boot: OK"
    else
        echo "Enabling cron service..."
        systemctl enable cron 2>/dev/null || systemctl enable crond 2>/dev/null || true
    fi

    if systemctl is-active cron >/dev/null 2>&1 || systemctl is-active crond >/dev/null 2>&1; then
        echo "cron service is active: OK"
    else
        echo "Starting cron service..."
        systemctl start cron 2>/dev/null || systemctl start crond 2>/dev/null || true
    fi
else
    echo "systemctl not found — please ensure cron is running manually."
fi

# ------------------------------------------------------------------
# Optional: install logrotate config
# ------------------------------------------------------------------

LOGROTATE_FILE="/etc/logrotate.d/webscrap-fx"
if [[ ! -f "$LOGROTATE_FILE" && -w "/etc/logrotate.d" ]]; then
    cat > "$LOGROTATE_FILE" <<'EOF'
/var/log/webscrap-fx-*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 644 root root
}
EOF
    echo "Logrotate config installed at $LOGROTATE_FILE"
elif [[ -f "$LOGROTATE_FILE" ]]; then
    echo "Logrotate config already exists at $LOGROTATE_FILE"
fi

echo
echo "Done. Logs: /var/log/webscrap-fx-*.log"
echo "Secrets:  $ENV_FILE (mode 600)"
echo "Mode:     $MODE"
