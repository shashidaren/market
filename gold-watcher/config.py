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
# Secrets are loaded from a .env file next to this config (or from
# environment variables). See .env.example for the required keys.
import os as _os

def _load_env(path=_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".env")):
    if _os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    _os.environ.setdefault(k.strip(), v.strip())

_load_env()

TELEGRAM_ENABLED    = True
TELEGRAM_TOKEN      = _os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID    = _os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Timezone ─────────────────────────────────────────────────
TIMEZONE            = "Asia/Kuala_Lumpur"

# ── Logging ──────────────────────────────────────────────────
LOG_FILE            = "logs/watcher.log"
LOG_LEVEL           = "INFO"
STATE_FILE          = "logs/alert_state.json"
