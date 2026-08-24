#!/usr/bin/env python3
"""
gold-watcher preflight check — verifies the watcher daemon can fire
Telegram alerts after the config change (token moved to .env).

Usage (on the server):

    python3 preflight_check.py                # read-only checks
    python3 preflight_check.py --send-test    # + sends a test alert through
                                              #   the real send_telegram()

Note: gold-watcher is a LONG-RUNNING daemon (not cron). After pulling the
new config.py you MUST create .env (see .env.example) and restart the
process, otherwise the next restart leaves it running with no token.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

OK, FAIL, WARN = "\033[92m[ OK ]\033[0m", "\033[91m[FAIL]\033[0m", "\033[93m[WARN]\033[0m"
failures = 0


def redact(text):
    text = str(text)
    for key in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
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


def check_env_file():
    env_path = HERE / ".env"
    report(env_path.exists(), ".env file exists",
           str(env_path) if env_path.exists()
           else f"MISSING — copy .env.example to {env_path} and fill in values. "
                "The watcher will run with NO token after restart until this exists!")
    import config
    report(bool(config.TELEGRAM_TOKEN), "config.TELEGRAM_TOKEN loaded",
           "set" if config.TELEGRAM_TOKEN else "EMPTY — .env missing or key wrong")
    report(bool(config.TELEGRAM_CHAT_ID), "config.TELEGRAM_CHAT_ID loaded",
           "set" if config.TELEGRAM_CHAT_ID else "EMPTY — .env missing or key wrong")


def check_deps():
    for mod in ("yfinance", "pytz", "requests"):
        try:
            __import__(mod)
            report(True, f"dependency: {mod}")
        except ImportError:
            report(False, f"dependency: {mod}", "pip install " + mod)


def check_process():
    try:
        out = subprocess.run(["pgrep", "-af", "watcher.py"],
                             capture_output=True, text=True).stdout.strip()
        # ignore our own preflight process
        lines = [l for l in out.splitlines() if "preflight" not in l]
        if lines:
            print(f"{OK} watcher process running — {lines[0][:100]}")
            print(f"{WARN}   note: if it started BEFORE the config change, restart it "
                  "after creating .env so it picks up the new config")
        else:
            print(f"{WARN} no watcher.py process found — daemon not running "
                  "(start it: nohup python3 watcher.py & — or better, a systemd unit)")
    except FileNotFoundError:
        print(f"{WARN} pgrep unavailable — check manually: ps aux | grep watcher.py")


def check_yahoo():
    import config
    sys.path.insert(0, str(HERE.parent))
    try:
        import yahoo_client
        info = yahoo_client.circuit_info()
        if info["open"]:
            print(f"{WARN} Yahoo circuit OPEN for {info['remaining']:.0f}s "
                  f"— {info['reason'] or 'no reason'}")
        df = yahoo_client.history(config.TICKER, period="2d", interval="1h")
        if df is None or df.empty:
            report(False, f"Yahoo Finance feed ({config.TICKER} COMEX futures)", "empty response")
            return None
        price = float(df["Close"].iloc[-1])
        report(True, f"Yahoo Finance feed ({config.TICKER} = COMEX gold futures, NOT spot XAU/USD)",
               f"latest {price:.2f} (expect $5–$20 basis vs. broker spot)")
        return price
    except Exception as e:
        report(False, "Yahoo Finance feed", str(e))
        return None


def check_telegram():
    import config
    import requests
    if not config.TELEGRAM_TOKEN:
        report(False, "Telegram getMe", "no token")
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/getMe",
                         timeout=10).json()
        report(r.get("ok", False), "Telegram getMe",
               f"bot @{r['result']['username']}" if r.get("ok") else str(r.get("description")))
    except Exception as e:
        report(False, "Telegram API", str(e))


def check_state(price):
    import config
    state_path = HERE / config.STATE_FILE
    if not state_path.exists():
        print(f"{WARN} state file missing ({state_path}) — fresh start, all targets re-armed")
        return
    try:
        state = json.loads(state_path.read_text())
        hit = state.get("targets_hit", [])
        print(f"{OK} state file — targets_hit={hit}, last_price={state.get('last_price')}")
        if price is not None:
            pending = [t["level"] for t in config.TARGETS
                       if t["level"] not in hit and price >= t["level"]]
            if pending:
                print(f"{WARN} price {price:.2f} is ABOVE un-hit target(s) {pending} — "
                      "a REAL alert will fire on the next watcher cycle")
    except Exception as e:
        report(False, "state file", str(e))


def send_test(price):
    from watcher import send_telegram
    p = f"${price:,.2f}" if price else "n/a"
    ok = send_telegram(
        "🧪 <b>gold-watcher preflight test</b>\n\n"
        f"Current gold: <b>{p}</b>\n"
        "This is a TEST alert — config/.env/Telegram path verified.\n"
        "Not a real target alert; state file untouched."
    )
    report(ok, "test alert delivery", "check your Telegram!" if ok else "send failed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-test", action="store_true", help="send a test Telegram alert")
    args = ap.parse_args()

    os.chdir(HERE)  # watcher/config use relative logs/ paths

    print("─" * 60)
    print("gold-watcher preflight check")
    print("─" * 60)
    check_env_file()
    check_deps()
    check_process()
    price = check_yahoo()
    check_telegram()
    check_state(price)
    if args.send_test:
        send_test(price)
    print("─" * 60)
    print("RESULT: " + ("all checks passed ✅" if failures == 0 else f"{failures} check(s) FAILED ❌"))
    sys.exit(1 if failures else 0)
