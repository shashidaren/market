# ═════════════════════════════════════════════════════════════
#  GOLD PRICE WATCHER — Configuration
# ═════════════════════════════════════════════════════════════

# ── Data Source ──────────────────────────────────────────────
# IMPORTANT: GC=F is the COMEX front-month gold FUTURES contract,
# NOT spot XAU/USD. The two diverge by $5–$20+ due to contango/
# backwardation and gap sharply on futures rollover dates (3rd-last
# business day of Jan/Mar/May/Jul/Sep/Nov, ~6× per year). We use
# GC=F because Yahoo Finance does not reliably serve spot XAU/USD;
# every alert carries a disclaimer.
TICKER              = "GC=F"        # COMEX gold futures (~spot proxy)
TICKER_LABEL        = "COMEX Gold Futures (GC=F)"
SPOT_DISCLAIMER     = ("Note: price is COMEX GC=F futures, not spot XAU/USD. "
                       "Expect $5–$20 basis vs. your broker; confirm spot.")
CHECK_INTERVAL_MIN  = 15            # Check every 15 minutes

# ── Rollover guard ───────────────────────────────────────────
# Suppress target-hit / drop alerts within N days of a COMEX roll
# date so a contract-switch gap doesn't fire a false alert.
ROLL_SUPPRESS_DAYS_BEFORE = 1
ROLL_SUPPRESS_DAYS_AFTER  = 1

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
                    _os.environ.setdefault(k.strip(), v.strip().strip('\'"'))

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
