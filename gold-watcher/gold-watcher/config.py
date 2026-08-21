# ═════════════════════════════════════════════════════════════
#  GOLD PRICE WATCHER — Configuration
# ═════════════════════════════════════════════════════════════

# ── Data Source ──────────────────────────────────────────────
TICKER              = "GC=F"        # Gold futures (~spot price)
CHECK_INTERVAL_MIN  = 15            # Check every 15 minutes

# ── Sell Targets (USD/oz) ────────────────────────────────────
# Alert fires ONCE when price crosses ABOVE each level
TARGETS = [
    {"level": 4500, "action": "Sell 25% — secure early profit"},
    {"level": 4750, "action": "Sell 25% — mid-target"},
    {"level": 5000, "action": "Sell 25% — original goal 🎯"},
    {"level": 5500, "action": "Sell 25% — stretch target 🚀"},
]

# ── Bonus Alerts ─────────────────────────────────────────────
DAILY_SUMMARY_ENABLED   = True
DAILY_SUMMARY_HOUR      = 8         # 8 AM Malaysia time

DROP_ALERT_ENABLED      = True
DROP_ALERT_PCT          = 3.0       # Alert if drops 3%+ in a day

# ── Telegram (reuse from EUR/USD dashboard) ──────────────────
TELEGRAM_ENABLED    = True
TELEGRAM_TOKEN      = "***REMOVED***"
TELEGRAM_CHAT_ID    = "***REMOVED***"

# ── Timezone ─────────────────────────────────────────────────
TIMEZONE            = "Asia/Kuala_Lumpur"

# ── Logging ──────────────────────────────────────────────────
LOG_FILE            = "logs/watcher.log"
LOG_LEVEL           = "INFO"
STATE_FILE          = "logs/alert_state.json"
