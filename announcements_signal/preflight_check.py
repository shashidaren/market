#!/usr/bin/env python3
"""
announcements_signal preflight check — verifies the Bursa insider-trade
pipeline can deliver Telegram alerts.

Usage (on the server):

    python3 preflight_check.py                # read-only checks
    python3 preflight_check.py --send-test    # + sends a test HTML alert
                                              #   through the real send path

IMPORTANT: this module's cron lines have NO BASH_ENV, so the bot has been
failing silently if credentials weren't in the environment. The config now
auto-loads .env from this folder — create it from .env.example.
"""

import argparse
import os
import sqlite3
import sys
from datetime import date, timedelta
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
    import announcements_config  # noqa: F401  (auto-loads .env)
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        report(bool(os.environ.get(key)), f"env: {key}",
               "set" if os.environ.get(key) else
               "MISSING — cp .env.example .env and fill in (cron has no BASH_ENV here!)")


def check_deps():
    for mod in ("cloudscraper", "requests", "yfinance"):
        try:
            __import__(mod)
            report(True, f"dependency: {mod}")
        except ImportError:
            report(False, f"dependency: {mod}", "pip install " + mod)


def check_bursa():
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper()
        r = scraper.get(
            "https://www.bursamalaysia.com/api/v1/announcements/search",
            params={"ann_type": "company", "per_page": 1, "page": 1},
            timeout=30,
        )
        ok = r.status_code == 200
        report(ok, "Bursa Malaysia API", f"HTTP {r.status_code}"
               + ("" if ok else " — Cloudflare may be blocking; try again"))
    except Exception as e:
        report(False, "Bursa Malaysia API", str(e))


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
    from collector import DB_PATH
    if not Path(DB_PATH).exists():
        report(False, "news.db", f"missing at {DB_PATH} — run collector.py first")
        return
    con = sqlite3.connect(DB_PATH)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("bursa_announcements", "bursa_insider_details"):
        report(t in tables, f"db table: {t}")
    if "bursa_announcements" in tables:
        n, latest = con.execute(
            "SELECT COUNT(*), MAX(published_date) FROM bursa_announcements").fetchone()
        stale = latest < (date.today() - timedelta(days=1)).isoformat() if latest else True
        print(f"{WARN if stale else OK} announcements — {n} rows, latest {latest}"
              + (" (stale — is the collector cron running?)" if stale else ""))
    if "bursa_insider_details" in tables:
        cols = [c[1] for c in con.execute("PRAGMA table_info(bursa_insider_details)")]
        if "delivered" in cols and "alert_ready" in cols:
            total = con.execute("SELECT COUNT(*) FROM bursa_insider_details "
                                "WHERE delivered=0 AND alert_ready=1").fetchone()[0]
            cutoff = (date.today() - timedelta(days=2)).isoformat()
            sendable = con.execute(
                """SELECT COUNT(*) FROM bursa_insider_details bid
                   JOIN bursa_announcements ba ON bid.ann_id = ba.ann_id
                   WHERE bid.delivered=0 AND bid.alert_ready=1
                   AND bid.published_date >= ? AND bid.transaction_type IS NOT NULL
                   AND ba.subcategory IN ('DIRECTOR_S219','SUBSTANTIAL_S138')""",
                (cutoff,)).fetchone()[0]
            print(f"{OK} insider queue — {total} undelivered total, "
                  f"{sendable} within the 2-day window")
            if sendable > 0:
                print(f"{WARN}   the next telegram_bot.py run will deliver alerts for "
                      f"these {sendable} row(s) (deduped per stock) — expect a burst")
    con.close()


def check_ireport():
    """Verify the i_report filter is operational (not just failing open)."""
    enabled = os.environ.get("IREPORT_FILTER", "on").lower() not in ("off", "0", "false", "no")
    fail_open = os.environ.get("IREPORT_FAIL_OPEN", "off").lower() in ("on", "1", "true", "yes")
    if not enabled:
        print(f"{WARN} i_report filter — DISABLED via IREPORT_FILTER=off (all alerts pass)")
        return
    if fail_open:
        print(f"{WARN} i_report filter — FAIL-OPEN (unavailable analysis will still alert)")
        return
    try:
        import ireport_filter
        ok = ireport_filter._load_engine()
        report(ok, "i_report filter engine",
               "loaded" if ok else f"unavailable ({ireport_filter._load_error}) — "
               "alerts will be deferred until i_report is healthy. pip install pandas-ta-classic")
        pf = ireport_filter.load_portfolio()
        print(f"{OK} portfolio.txt — {len(pf)} holding(s): {sorted(pf) if pf else '(none — see portfolio.txt.example)'}")
    except Exception as e:
        report(False, "i_report filter", str(e))


def send_test():
    from telegram_bot import send_telegram_message
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    ok = send_telegram_message(
        token, chat,
        "🧪 <b>announcements_signal preflight test</b>\n\n"
        "Bursa insider-alert delivery path verified.\n"
        "This is a TEST — no real announcement, queue untouched."
    )
    report(ok, "test alert delivery", "check your Telegram!" if ok else "send failed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-test", action="store_true", help="send a test Telegram alert")
    args = ap.parse_args()

    print("─" * 60)
    print("announcements_signal preflight check")
    print("─" * 60)
    check_env()
    check_deps()
    check_bursa()
    check_telegram()
    check_db()
    check_ireport()
    if args.send_test:
        send_test()
    print("─" * 60)
    print("RESULT: " + ("all checks passed ✅" if failures == 0 else f"{failures} check(s) FAILED ❌"))
    sys.exit(1 if failures else 0)
