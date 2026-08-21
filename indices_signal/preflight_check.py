#!/usr/bin/env python3
"""
indices_signal preflight check — verifies the pipeline can fire Telegram
signals without waiting for a real 4H setup.

Usage (on the server):

    python3 preflight_check.py                # read-only checks
    python3 preflight_check.py --send-test    # + sends a FAKE formatted GOLD
                                              #   signal through the real
                                              #   format_message + send_telegram
                                              #   (NOT logged to signals_sent)

Note: the real pipeline entry point is telegram_bot.py (engine runs inside
it). Its cron line is currently commented out in crontab — re-enable it
after this preflight passes.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent

OK, FAIL, WARN = "\033[92m[ OK ]\033[0m", "\033[91m[FAIL]\033[0m", "\033[93m[WARN]\033[0m"
failures = 0


def redact(text):
    text = str(text)
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
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


def check_env():
    # importing indices_config auto-loads .env
    import indices_config  # noqa: F401
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        report(bool(os.environ.get(key)), f"env: {key}",
               "set" if os.environ.get(key) else "MISSING — check .env")


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
        df = yf.Ticker("GC=F").history(period="1d", interval="1h")
        report(not df.empty, "Yahoo Finance feed (GC=F gold)",
               f"latest close {df['Close'].iloc[-1]:.2f}" if not df.empty else "empty response")
        return None if df.empty else float(df["Close"].iloc[-1])
    except Exception as e:
        report(False, "Yahoo Finance feed", str(e))
        return None


def check_telegram():
    import requests
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        report(False, "Telegram getMe", "no token")
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10).json()
        report(r.get("ok", False), "Telegram getMe",
               f"bot @{r['result']['username']}" if r.get("ok") else str(r.get("description")))
    except Exception as e:
        report(False, "Telegram API", str(e))


def check_db():
    from indices_config import DB_PATH
    db = Path(DB_PATH) if Path(DB_PATH).exists() else HERE / "prices.db"
    if not db.exists():
        report(False, "prices.db", "missing — run price_collector.py first")
        return
    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("prices", "signals_sent"):
        report(t in tables, f"db table: {t}")
    if "prices" in tables:
        for tf, latest in con.execute(
                "SELECT timeframe, MAX(datetime) FROM prices GROUP BY timeframe"):
            n = con.execute("SELECT COUNT(*) FROM prices WHERE timeframe=?", (tf,)).fetchone()[0]
            print(f"{OK} db prices[{tf}] — {n} bars, latest {latest}")
    if "signals_sent" in tables:
        n = con.execute("SELECT COUNT(*) FROM signals_sent").fetchone()[0]
        print(f"{OK} signals_sent — {n} signal(s) previously delivered")
    con.close()


def send_test_signal(live_price):
    """Send a FAKE gold signal through the real formatting + delivery path."""
    from telegram_bot import format_message, send_telegram
    price = live_price or 4500.0
    sig = {
        "ticker": "GOLD",
        "type": "BUY",
        "price": price,
        "sl": price - 30.0,
        "tp": price + 60.0,
        "score": 88,
        "daily_trend": "UP",
        "rsi": 55.0,
        "adx": 30.0,
        "atr": 15.0,
        "bar_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "reasons": [
            "🧪 THIS IS A TEST SIGNAL from preflight_check.py",
            "NOT a real setup — do not trade",
            "verifies formatting + Telegram delivery end-to-end",
        ],
    }
    ok = send_telegram(format_message(sig))
    report(ok, "test signal delivery",
           "check your Telegram! (not logged to signals_sent)" if ok else "send failed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-test", action="store_true",
                    help="send a fake formatted GOLD signal to Telegram")
    args = ap.parse_args()

    print("─" * 60)
    print("indices_signal preflight check")
    print("─" * 60)
    check_env()
    check_deps()
    live = check_yahoo()
    check_telegram()
    check_db()
    if args.send_test:
        send_test_signal(live)
    print("─" * 60)
    print("RESULT: " + ("all checks passed ✅" if failures == 0 else f"{failures} check(s) FAILED ❌"))
    sys.exit(1 if failures else 0)
