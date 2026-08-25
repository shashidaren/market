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
it). Re-enable the cron only after this preflight passes.
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
    detail = redact(detail)[:200]
    print(f"{OK if ok else FAIL} {label}" + (f" — {detail}" if detail else ""))


def warn(label, detail=""):
    detail = redact(detail)[:200]
    print(f"{WARN} {label}" + (f" — {detail}" if detail else ""))


def check_env():
    # importing indices_config auto-loads .env
    import indices_config  # noqa: F401
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        report(bool(os.environ.get(key)), f"env: {key}",
               "set" if os.environ.get(key) else "MISSING — check .env")


def check_config():
    """Surface signal-quality knobs so live config is obvious."""
    from indices_config import SIGNAL_CONFIG, TICKERS

    report(
        SIGNAL_CONFIG.get("min_score", 0) >= 75,
        "config: min_score",
        str(SIGNAL_CONFIG.get("min_score")),
    )
    align = SIGNAL_CONFIG.get("require_trend_alignment", False)
    print(
        f"{OK} config: require_trend_alignment — {align}"
        + (" (stricter; off until outcomes justify)" if not align else "")
    )
    flips = SIGNAL_CONFIG.get("flag_quick_flips", True)
    print(f"{OK} config: flag_quick_flips — {flips}")
    print(
        f"{OK} config: RSI veto BUY>{SIGNAL_CONFIG.get('rsi_veto_buy_above', 70)} "
        f"SELL<{SIGNAL_CONFIG.get('rsi_veto_sell_below', 30)} | "
        f"ADX≥{SIGNAL_CONFIG.get('min_adx')} | "
        f"cooldown {SIGNAL_CONFIG.get('cooldown_hours')}h | "
        f"max_bar_age {SIGNAL_CONFIG.get('max_bar_age_hours')}h"
    )
    print(f"{OK} config: tickers — {', '.join(TICKERS.keys())}")


def check_deps():
    for mod in ("yfinance", "pandas", "requests", "numpy"):
        try:
            __import__(mod)
            report(True, f"dependency: {mod}")
        except ImportError:
            report(False, f"dependency: {mod}", "pip install " + mod)


def check_yahoo():
    sys.path.insert(0, str(HERE.parent))
    try:
        import yahoo_client
        info = yahoo_client.circuit_info()
        if info["open"]:
            warn(
                "Yahoo circuit OPEN",
                f"{info['remaining']:.0f}s remaining — {info['reason'] or 'no reason'}",
            )
        result = yahoo_client.probe("GC=F")
        report(
            result["ok"],
            "Yahoo Finance feed (GC=F COMEX gold futures)",
            (
                f"latest close {result['close']:.2f} (NOT spot XAU/USD — expect $5–$20 basis)"
                if result["ok"]
                else result["error"]
            ),
        )
        return result.get("close") if result.get("ok") else None
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
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getMe", timeout=10
        ).json()
        report(
            r.get("ok", False),
            "Telegram getMe",
            f"bot @{r['result']['username']}" if r.get("ok") else str(r.get("description")),
        )
    except Exception as e:
        report(False, "Telegram API", str(e))


def check_db():
    from indices_config import DB_PATH, SIGNAL_CONFIG, TICKERS, PRIMARY_TIMEFRAME, TREND_TIMEFRAME
    from signal_engine import ensure_schema

    db = Path(DB_PATH)
    if not db.exists():
        report(False, "prices.db", "missing — run price_collector.py first")
        return

    # Ensure outcome / quick_flip columns exist (idempotent migration).
    try:
        ensure_schema(str(db))
        report(True, "db schema", "ensure_schema ok (sl/tp/atr/outcome/quick_flip)")
    except Exception as e:
        report(False, "db schema ensure_schema", str(e))

    con = sqlite3.connect(db)
    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for t in ("prices", "signals_sent"):
        report(t in tables, f"db table: {t}")

    if "signals_sent" in tables:
        cols = {r[1] for r in con.execute("PRAGMA table_info(signals_sent)")}
        for col in ("sl", "tp", "atr", "outcome", "outcome_price", "resolved_at", "quick_flip"):
            report(col in cols, f"db signals_sent.{col}")
        n = con.execute("SELECT COUNT(*) FROM signals_sent").fetchone()[0]
        resolved = con.execute(
            "SELECT COUNT(*) FROM signals_sent WHERE outcome IS NOT NULL"
        ).fetchone()[0]
        print(f"{OK} signals_sent — {n} total, {resolved} resolved")

    if "prices" not in tables:
        con.close()
        return

    min_4h = SIGNAL_CONFIG.get("min_4h_candles", 100)
    min_1d = SIGNAL_CONFIG.get("min_1d_candles", 50)
    vol_need = SIGNAL_CONFIG.get("volume_baseline_bars", 30)

    for ticker in TICKERS:
        n4 = con.execute(
            "SELECT COUNT(*) FROM prices WHERE ticker=? AND timeframe=?",
            (ticker, PRIMARY_TIMEFRAME),
        ).fetchone()[0]
        n1 = con.execute(
            "SELECT COUNT(*) FROM prices WHERE ticker=? AND timeframe=?",
            (ticker, TREND_TIMEFRAME),
        ).fetchone()[0]
        latest4 = con.execute(
            "SELECT MAX(datetime) FROM prices WHERE ticker=? AND timeframe=?",
            (ticker, PRIMARY_TIMEFRAME),
        ).fetchone()[0]
        ok_depth = n4 >= min_4h and n1 >= min_1d
        report(
            ok_depth,
            f"db depth {ticker}",
            f"4H={n4} (need ≥{min_4h}), 1d={n1} (need ≥{min_1d}), latest 4H={latest4}",
        )

        if ticker == "GOLD":
            # Volume veto + assertion need non-null volume history.
            n_vol = con.execute(
                """
                SELECT COUNT(*) FROM prices
                WHERE ticker=? AND timeframe=? AND volume IS NOT NULL AND volume > 0
                """,
                (ticker, PRIMARY_TIMEFRAME),
            ).fetchone()[0]
            report(
                n_vol >= vol_need,
                "db GOLD 4H volume",
                f"{n_vol} bars with volume (need ≥{vol_need} for volume veto)",
            )

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
        "daily_trend": "BULL",
        "rsi": 55.0,
        "adx": 30.0,
        "atr": 15.0,
        "bar_time": datetime.now(timezone.utc),
        "reasons": [
            "🧪 THIS IS A TEST SIGNAL from the preflight script",
            "NOT a real setup — do not trade",
            "verifies formatting + Telegram delivery end-to-end",
        ],
        # Exercise the quick-flip warning path without touching the DB.
        "quick_flip": {
            "prior_type": "SELL",
            "prior_score": 80,
            "hours_ago": 3.5,
        },
    }
    msg = format_message(sig)
    if "QUICK FLIP" not in msg:
        warn("test message", "quick-flip banner missing from format_message output")
    ok = send_telegram(msg)
    report(
        ok,
        "test signal delivery",
        "check your Telegram! (not logged to signals_sent)" if ok else "send failed",
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--send-test",
        action="store_true",
        help="send a fake formatted GOLD signal to Telegram",
    )
    args = ap.parse_args()

    print("─" * 60)
    print("indices_signal preflight check")
    print("─" * 60)
    check_env()
    check_config()
    check_deps()
    live = check_yahoo()
    check_telegram()
    check_db()
    if args.send_test:
        send_test_signal(live)
    print("─" * 60)
    print(
        "RESULT: "
        + (
            "all checks passed ✅"
            if failures == 0
            else f"{failures} check(s) FAILED ❌"
        )
    )
    sys.exit(1 if failures else 0)
