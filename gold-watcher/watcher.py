import calendar
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytz
import requests

import config

# ── GC=F rollover detection (standalone — same logic as indices_signal) ──
# COMEX gold rolls 6×/yr: Feb/Apr/Jun/Aug/Oct/Dec contracts. The roll
# day is the 3rd-last business day of the PRIOR month. We use this to
# suppress alerts in a ±day window when the front-month gap can trigger
# false "target hit" or "3% drop" alerts.
_GC_ROLL_MONTHS = (2, 4, 6, 8, 10, 12)


def _gc_roll_dates(year: int):
    dates = []
    for cm in _GC_ROLL_MONTHS:
        pm = cm - 1
        y = year
        if pm == 0:
            pm, y = 12, year - 1
        last = calendar.monthrange(y, pm)[1]
        d = datetime(y, pm, last, tzinfo=timezone.utc)
        bd = 0
        target = None
        while bd < 3:
            if d.weekday() < 5:
                bd += 1
                if bd == 3:
                    target = d
                    break
            d -= timedelta(days=1)
        if target:
            dates.append(target)
    return dates


def near_gc_roll(now=None):
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    before = config.ROLL_SUPPRESS_DAYS_BEFORE
    after  = config.ROLL_SUPPRESS_DAYS_AFTER
    for y in (now.year - 1, now.year, now.year + 1):
        for roll in _gc_roll_dates(y):
            if roll - timedelta(days=before) <= now <= roll + timedelta(days=after):
                return True, roll
    return False, None




_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
import yahoo_client  # noqa: E402

# ── Logging Setup ────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        RotatingFileHandler(config.LOG_FILE, maxBytes=5*1024*1024, backupCount=3),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── State Management ─────────────────────────────────────────

def load_state():
    if not os.path.exists(config.STATE_FILE):
        return {
            "targets_hit":    [],     # list of levels already alerted
            "last_summary":   None,   # date string of last daily summary
            "last_drop":      None,   # date of last drop alert
            "last_roll_note": None,   # date of last roll-window advisory
            "last_price":     None,
        }
    try:
        with open(config.STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"targets_hit": [], "last_summary": None, "last_drop": None, "last_price": None}


def save_state(state):
    with open(config.STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Fetch Gold Price ─────────────────────────────────────────

def fetch_gold_data():
    """Return dict with current, previous close, and daily change."""
    if yahoo_client.is_circuit_open():
        info = yahoo_client.circuit_info()
        log.warning(
            "Yahoo circuit OPEN for %.0fs (%s) — skipping cycle",
            info["remaining"], info["reason"] or "no reason",
        )
        return None
    try:
        df = yahoo_client.history(config.TICKER, period="2d", interval="1h")
        if df is None or df.empty:
            log.error("No data returned")
            return None

        current   = float(df["Close"].iloc[-1])
        prev_day  = float(df["Close"].iloc[0])
        change    = current - prev_day
        change_pct = (change / prev_day) * 100

        return {
            "price":       round(current, 2),
            "prev_close":  round(prev_day, 2),
            "change":      round(change, 2),
            "change_pct":  round(change_pct, 2),
        }
    except Exception as e:
        log.error("Fetch error: %s", e)
        return None


# ── Telegram Send ────────────────────────────────────────────

def send_telegram(message):
    if not config.TELEGRAM_ENABLED:
        return False

    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  config.TELEGRAM_CHAT_ID,
        "text":                     message,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            log.info("✅ Alert sent")
            return True
        log.error("Telegram failed: %s %s", r.status_code, r.text)
        return False
    except Exception as e:
        log.error("Telegram error: %s", e)
        return False


# ── Alert Builders ───────────────────────────────────────────

def _next_target(price, state):
    """Return next upcoming target above current price."""
    remaining = [t for t in config.TARGETS if t["level"] not in state["targets_hit"]]
    if not remaining:
        return None
    return min(remaining, key=lambda t: t["level"])


def build_target_alert(target, data):
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z")

    in_roll, roll_dt = near_gc_roll()
    roll_warn = (
        f"\n🚨 <b>Futures roll window</b> (roll date ~{roll_dt.strftime('%Y-%m-%d')}) — "
        f"price gap risk elevated; verify on your broker before acting."
        if in_roll else ""
    )

    return f"""🥇 <b>GOLD TARGET HIT!</b> 🚨

💰 <b>Current Price:</b>  <code>${data['price']:,.2f}</code> /oz ({config.TICKER_LABEL})
🎯 <b>Target Hit:</b>     <code>${target['level']:,.2f}</code>
📈 <b>Daily Change:</b>   {data['change_pct']:+.2f}%

💡 <b>Action:</b>
   {target['action']}
{roll_warn}
⚠️ <b>Data source:</b> {config.SPOT_DISCLAIMER}

⏰ <b>Time:</b> {now}

━━━━━━━━━━━━━━━━━━━━
🔗 <a href="https://versa.com.my">Open Versa App</a>
"""


def build_daily_summary(data, state):
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z")

    # Progress bar
    next_t = _next_target(data["price"], state)

    if next_t:
        distance     = next_t["level"] - data["price"]
        distance_pct = (distance / data["price"]) * 100
        next_line = (
            f"🎯 <b>Next Target:</b>  <code>${next_t['level']:,.2f}</code>\n"
            f"   Distance:  <code>${distance:,.2f}</code>  ({distance_pct:+.2f}%)"
        )
    else:
        next_line = "🏆 <b>All targets hit!</b> Time to plan next moves."

    # Targets progress
    target_lines = []
    for t in config.TARGETS:
        if t["level"] in state["targets_hit"]:
            target_lines.append(f"   ✅ ${t['level']:,}  hit")
        else:
            target_lines.append(f"   ⏳ ${t['level']:,}  pending")

    change_emoji = "📈" if data["change_pct"] >= 0 else "📉"
    in_roll, roll_dt = near_gc_roll()
    roll_line = (
        f"\n🚨 <b>Futures roll window</b> (≈{roll_dt.strftime('%Y-%m-%d')}) — "
        f"expect gaps/basis noise." if in_roll else ""
    )

    return f"""☀️ <b>Daily Gold Summary</b>

💰 <b>Price Now:</b>       <code>${data['price']:,.2f}</code> /oz ({config.TICKER_LABEL})
{change_emoji} <b>Daily Change:</b>    {data['change_pct']:+.2f}%  (<code>${data['change']:+,.2f}</code>)

{next_line}

📊 <b>Target Progress:</b>
<pre>{chr(10).join(target_lines)}</pre>
{roll_line}
⚠️ <b>Data source:</b> {config.SPOT_DISCLAIMER}

⏰ <b>Time:</b> {now}
"""


def build_drop_alert(data):
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z")

    in_roll, roll_dt = near_gc_roll()
    roll_warn = (
        f"\n🚨 <b>Futures roll window</b> (≈{roll_dt.strftime('%Y-%m-%d')}) — "
        f"the drop may be a contract-roll gap, not a real spot move. "
        f"Verify on your broker before acting."
        if in_roll else ""
    )

    return f"""📉 <b>GOLD DROP ALERT</b>

⚠️ Gold dropped <b>{data['change_pct']:.2f}%</b> today ({config.TICKER_LABEL})

💰 <b>Current Price:</b>  <code>${data['price']:,.2f}</code> /oz
📊 <b>Change:</b>         <code>${data['change']:,.2f}</code>
{roll_warn}
💡 <b>Potential buying opportunity</b>
   Consider dollar-cost averaging into position
⚠️ <b>Data source:</b> {config.SPOT_DISCLAIMER}

⏰ <b>Time:</b> {now}
"""


# ── Main Check Logic ─────────────────────────────────────────

def check_price():
    state = load_state()
    data  = fetch_gold_data()

    if not data:
        log.warning("Skipping cycle — no data")
        return

    log.info("Gold: $%.2f  (%+.2f%%)  targets_hit=%s",
             data["price"], data["change_pct"], state["targets_hit"])

    tz  = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz)
    today_str = now.strftime("%Y-%m-%d")

    # ── 0. Futures roll-window guard ─────────────────────────
    # Around COMEX roll days the GC=F price can gap $10–$30 purely
    # from a contract switch; suppress target/drop alerts so we
    # don't act on a data artifact. Daily summary still sends (it
    # is informational and carries the disclaimer).
    in_roll, roll_dt = near_gc_roll()
    roll_alerted_today = state.get("last_roll_note") == today_str
    if in_roll:
        log.warning("GC=F futures roll window active (roll≈%s) — "
                    "suppressing target/drop alerts",
                    roll_dt.strftime("%Y-%m-%d") if roll_dt else "?")

    # ── 1. Target crossed check ──────────────────────────────
    for target in config.TARGETS:
        if target["level"] in state["targets_hit"]:
            continue
        if data["price"] >= target["level"]:
            if in_roll:
                log.info("🎯 Target $%s crossed but roll window active — alert suppressed",
                         target["level"])
                continue
            msg = build_target_alert(target, data)
            if send_telegram(msg):
                state["targets_hit"].append(target["level"])
                log.info("🎯 Target $%s hit and alerted", target["level"])

    # ── 2. Daily summary (once per day at set hour) ──────────
    if config.DAILY_SUMMARY_ENABLED:
        if now.hour == config.DAILY_SUMMARY_HOUR and state.get("last_summary") != today_str:
            msg = build_daily_summary(data, state)
            if send_telegram(msg):
                state["last_summary"] = today_str
                log.info("☀️ Daily summary sent")

    # ── 3. Drop alert (once per day) ─────────────────────────
    if config.DROP_ALERT_ENABLED:
        if data["change_pct"] <= -config.DROP_ALERT_PCT and state.get("last_drop") != today_str:
            if in_roll:
                log.info("📉 Drop threshold met (%.2f%%) but roll window active — alert suppressed",
                         data["change_pct"])
            else:
                msg = build_drop_alert(data)
                if send_telegram(msg):
                    state["last_drop"] = today_str
                    log.info("📉 Drop alert sent")

    # ── 3b. One-time roll advisory per day ───────────────────
    # So users know target/drop alerts are intentionally silent.
    if in_roll and not roll_alerted_today:
        note = (f"ℹ️ <b>GC=F Futures Roll Window</b>\n\n"
                f"Roll date ≈ <b>{roll_dt.strftime('%Y-%m-%d')}</b> UTC. "
                f"Target-hit and drop alerts are PAUSED during this window to "
                f"avoid false triggers from contract-switch gaps.\n"
                f"Daily summaries continue (with disclaimer).\n\n"
                f"⚠️ Verify spot XAU/USD on your broker before trading.")
        if send_telegram(note):
            state["last_roll_note"] = today_str
            log.info("ℹ️ Roll-window advisory sent")

    # ── Save state ───────────────────────────────────────────
    state["last_price"] = data["price"]
    save_state(state)


# ── Main Loop ────────────────────────────────────────────────

def main():
    log.info("🥇 Gold Watcher started")
    log.info("Targets: %s", [t["level"] for t in config.TARGETS])
    log.info("Check interval: %d min", config.CHECK_INTERVAL_MIN)

    while True:
        try:
            check_price()
        except Exception as e:
            log.error("Check cycle error: %s", e)

        time.sleep(config.CHECK_INTERVAL_MIN * 60)


if __name__ == "__main__":
    main()
