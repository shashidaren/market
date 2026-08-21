#!/bin/bash
# /opt/market/indices_signal/install_cron.sh

CRON_CMD="*/30 * * * * BASH_ENV=/opt/market/indices_signal/.env /opt/market/indices_signal/run_pipeline.sh"

(
    crontab -l 2>/dev/null \
    | sed '/# --- indices-signal-pipeline ---/,/# --- end indices-signal-pipeline ---/d'

    echo "# --- indices-signal-pipeline ---"
    echo "$CRON_CMD"
    echo "# --- end indices-signal-pipeline ---"
) | crontab -

echo "✅ Indices signal cron installed — runs every 30 minutes"
echo
crontab -l | sed -n '/# --- indices-signal-pipeline ---/,/# --- end indices-signal-pipeline ---/p'
