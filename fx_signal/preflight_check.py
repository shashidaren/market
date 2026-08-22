#!/usr/bin/env python3
"""
fx_signal preflight check — verifies the whole pipeline can fire Telegram
signals, without waiting days for a real crossover.

Usage (run on the server where the cron pipeline lives):

    python3 preflight_check.py                     # read-only checks
    python3 preflight_check.py --send-test         # + sends a plain test message
    python3 preflight_check.py --inject-test-signal # + queues a fake signal so
                                                    #   telegram_bot delivers it

Typical full test:
    1. python3 preflight_check.py --send-test
       -> confirms token/chat/network are good
    2. python3 preflight_check.py --inject-test-signal
    3. python3 telegram_bot.py        (or wait for the next cron cycle)
       -> the TEST signal should arrive in Telegram with full formatting
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "prices.db"

sys.path.insert(0, str(HERE))
from util import to_db_str, utc_now_str  # noqa: E402

OK, FAIL, WARN = "\033[92m[ OK ]\033[0m", "\033[91m[FAIL]\033[0m", "\033[93m[WARN]\033[0m"
failures = 0


def redact(text):
    """Strip secret values out of error messages before printing."""
    text = str(text)
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FINNHUB_API_KEY"):
        val = os.environ.get(key, "")
        if val and val in text:
            text = text.replace(val, "***" + key + "***")
    return text


def report(ok, label, detail=""):
    global failures
    if not ok:
        failures += 1
    detail = redact(detail)[:160]
    print(f"{OK if ok else FAIL} {label}" + (f" — {detail}" if detail else ""))


def load_env():
    env_path = HERE / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def check_env():
    load_env()
    for key, required in [("TELEGRAM_BOT_TOKEN", True), ("TELEGRAM_CHAT_ID", True),
                          ("SIGNAL_MODE", False), ("FINNHUB_API_KEY", False)]:
        val = os.environ.get(key, "")
        if required:
            report(bool(val), f"env: {key}", "set" if val else "MISSING — check .env")
        else:
            print(f"{OK if val else WARN} env: {key} — {'set' if val else 'not set (optional)'}")


def check_deps():
    for mod in ("yfinance", "pandas", "requests"):
        try:
            __import__(mod)
            report(True, f"dependency: {mod}")
        except ImportError:
            report(False, f"dependency: {mod}", "pip install " + mod)


def check_yahoo():
    try:
        import yfinance as yf
        df = yf.Ticker("EURUSD=X").history(period="1d", interval="1h")
        report(not df.empty, "Yahoo Finance feed (EURUSD=X)",
               f"latest close {df['Close'].iloc[-1]:.5f}" if not df.empty else "empty response")
        return None if df.empty else float(df["Close"].iloc[-1])
    except Exception as e:
        report(False, "Yahoo Finance feed", str(e))
        return None


def check_telegram(send_test=False):
    import requests
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token:
        report(False, "Telegram getMe", "no token")
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10).json()
        report(r.get("ok", False), "Telegram getMe",
               f"bot @{r['result']['username']}" if r.get("ok") else str(r.get("description")))
        if send_test and r.get("ok"):
            r2 = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                               json={"chat_id": chat,
                                     "text": "✅ fx_signal preflight — Telegram delivery works"},
                               timeout=10).json()
            report(r2.get("ok", False), "Telegram sendMessage (test)",
                   "check your Telegram!" if r2.get("ok") else str(r2.get("description")))
    except Exception as e:
        report(False, "Telegram API", str(e))


def check_finnhub():
    import requests
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        print(f"{WARN} Finnhub — key not set, calendar checks disabled (optional)")
        return
    try:
        r = requests.get("https://finnhub.io/api/v1/quote",
                         params={"symbol": "AAPL", "token": key}, timeout=10)
        report(r.status_code == 200, "Finnhub API key",
               "valid" if r.status_code == 200 else f"HTTP {r.status_code}")
    except Exception as e:
        report(False, "Finnhub API", str(e))


def check_db():
    if not DB.exists():
        report(False, "prices.db", "missing — run price_collector.py first")
        return
    con = sqlite3.connect(DB)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("price_signals", "signals", "signal_outcomes"):
        report(t in tables, f"db table: {t}")
    if "price_signals" in tables:
        row = con.execute("SELECT MAX(bar_time) FROM price_signals WHERE timeframe='1h'").fetchone()
        latest = row[0] or "none"
        fresh = False
        if row[0]:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row[0])
            fresh = age < timedelta(hours=2)
            print(f"{OK if fresh else WARN} db freshness — latest 1h bar {latest} "
                  f"({int(age.total_seconds()//60)} min old"
                  + ("" if fresh else "; engine will skip stale bars — is cron running?") + ")")
    if "signals" in tables:
        n = con.execute("SELECT COUNT(*) FROM signals WHERE delivered=0").fetchone()[0]
        print(f"{OK} signal queue — {n} undelivered signal(s) waiting")
    con.close()


def inject_test_signal(live_price):
    if live_price is None:
        report(False, "inject test signal", "needs a live Yahoo price — fix Yahoo check first")
        return
    pip = 0.0001
    entry = live_price
    sl, tp = entry - 30 * pip, entry + 60 * pip   # BUY, R:R = 2.0 (> MIN_LIVE_RR)
    con = sqlite3.connect(DB)
    cur = con.execute(
        """INSERT INTO signals (pair, direction, entry, stop_loss, take_profit,
                                atr_1h, rsi_1h, rationale, generated_at, delivered, expires_at)
           VALUES (?,?,?,?,?,?,?,?,?,0,?)""",
        ("EURUSD", "BUY", entry, sl, tp, 30 * pip, 55.0,
         "🧪 TEST SIGNAL (preflight_check.py) — NOT a real setup, do not trade",
         utc_now_str(),
         to_db_str(datetime.now(timezone.utc) + timedelta(hours=1))))
    con.commit()
    con.close()
    report(True, "inject test signal",
           f"signal id={cur.lastrowid} EURUSD BUY @ {entry:.5f} queued — "
           "now run: python3 telegram_bot.py (it should fire to Telegram)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-test", action="store_true", help="send a test Telegram message")
    ap.add_argument("--inject-test-signal", action="store_true",
                    help="queue a fake EURUSD signal for telegram_bot.py to deliver")
    args = ap.parse_args()

    print("─" * 60)
    print("fx_signal preflight check")
    print("─" * 60)
    check_env()
    check_deps()
    live = check_yahoo()
    check_telegram(send_test=args.send_test)
    check_finnhub()
    check_db()
    if args.inject_test_signal:
        inject_test_signal(live)
    print("─" * 60)
    print("RESULT: " + ("all checks passed ✅" if failures == 0 else f"{failures} check(s) FAILED ❌"))
    sys.exit(1 if failures else 0)
