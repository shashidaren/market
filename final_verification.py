#!/usr/bin/env python3
"""
Final verification for strict mode + confidence filter changes.

Run after every change:
    python3 final_verification.py

Checks:
  1. fx_config.py defaults
  2. install_cron.sh no longer forces relaxed
  3. telegram_bot.py has confidence gate
  4. .env.example documents new vars
  5. Functional test: 55% filtered, 77% passes
  6. Cron cleanliness (if crontab available)
  7. Module preflights (optional)

Exit 0 = all good, 1 = failures.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
FX = ROOT / "fx_signal"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
NC = "\033[0m"

PASS = []
FAIL = []
WARN = []

def ok(label, detail=""):
    print(f"{GREEN}[ PASS ]{NC} {label}" + (f" — {detail}" if detail else ""))
    PASS.append(label)

def fail(label, detail=""):
    print(f"{RED}[ FAIL ]{NC} {label}" + (f" — {detail}" if detail else ""))
    FAIL.append(label)

def warn(label, detail=""):
    print(f"{YELLOW}[ WARN ]{NC} {label}" + (f" — {detail}" if detail else ""))
    WARN.append(label)

def check_file_contains(path, pattern, label, should_contain=True):
    try:
        text = path.read_text()
        # Use DOTALL so .* matches newlines for multiline imports
        found = re.search(pattern, text, re.MULTILINE | re.DOTALL) is not None
        if should_contain and found:
            ok(label)
            return True
        elif not should_contain and not found:
            ok(label)
            return True
        else:
            fail(label, f"{'should contain' if should_contain else 'should NOT contain'} {pattern!r} in {path.name}")
            return False
    except Exception as e:
        fail(label, f"read error {path}: {e}")
        return False

print(f"{BOLD}=== FINAL VERIFICATION: strict + 60% confidence ==={NC}\n")

# 1. fx_config.py
print(f"{BOLD}-- fx_config.py --{NC}")
cfg_path = FX / "fx_config.py"
check_file_contains(cfg_path, r'SIGNAL_MODE\s*=\s*"strict"', "fx_config SIGNAL_MODE defaults to strict")
check_file_contains(cfg_path, r'MIN_CONFIDENCE\s*=\s*int.*60', "fx_config MIN_CONFIDENCE default 60")
check_file_contains(cfg_path, r'"MIN_CONFIDENCE"', "fx_config __all__ exports MIN_CONFIDENCE")
check_file_contains(cfg_path, r'0\s*<=\s*MIN_CONFIDENCE\s*<=\s*100', "fx_config validates MIN_CONFIDENCE range")

try:
    sys.path.insert(0, str(FX))
    from fx_config import SIGNAL_MODE, MIN_CONFIDENCE, validate_config, MIN_LIVE_RR
    if SIGNAL_MODE == "strict":
        ok("fx_config runtime SIGNAL_MODE == strict", f"{SIGNAL_MODE}")
    else:
        fail("fx_config runtime SIGNAL_MODE == strict", f"got {SIGNAL_MODE}")

    if MIN_CONFIDENCE == 60:
        ok("fx_config runtime MIN_CONFIDENCE == 60", f"{MIN_CONFIDENCE}")
    else:
        fail("fx_config runtime MIN_CONFIDENCE == 60", f"got {MIN_CONFIDENCE}")

    validate_config()
    ok("fx_config.validate_config() passes")
except Exception as e:
    fail("fx_config import/validate", str(e))

# 2. install_cron.sh
print(f"\n{BOLD}-- install_cron.sh --{NC}")
cron_install = FX / "install_cron.sh"
check_file_contains(cron_install, r'SIGNAL_MODE=relaxed', "install_cron.sh must NOT force relaxed", should_contain=False)
check_file_contains(cron_install, r'SIGNAL_MODE=strict', "install_cron.sh forces strict in legacy mode")
check_file_contains(cron_install, r'BASH_ENV.*run_pipeline', "install_cron.sh pipeline uses BASH_ENV .env")

# 3. telegram_bot.py
print(f"\n{BOLD}-- telegram_bot.py --{NC}")
bot_path = FX / "telegram_bot.py"
check_file_contains(bot_path, r'from fx_config import.*MIN_CONFIDENCE', "telegram_bot imports MIN_CONFIDENCE")
check_file_contains(bot_path, r'is_low_conf.*=.*confidence.*<.*MIN_CONFIDENCE', "telegram_bot computes is_low_conf")
check_file_contains(bot_path, r'is_low_conf.*bool', "telegram_bot format_message has is_low_conf param")
check_file_contains(bot_path, r'LOW CONFIDENCE.*MIN_CONFIDENCE', "telegram_bot banner shows LOW CONFIDENCE")
check_file_contains(bot_path, r'is_suppress or is_untradeable or is_low_conf', "telegram_bot suppression includes low confidence")

# 4. .env.example
print(f"\n{BOLD}-- .env.example --{NC}")
env_ex = FX / ".env.example"
check_file_contains(env_ex, r'SIGNAL_MODE=strict', ".env.example documents SIGNAL_MODE=strict")
check_file_contains(env_ex, r'MIN_CONFIDENCE=60', ".env.example documents MIN_CONFIDENCE")

# 5. Functional test: confidence filter
print(f"\n{BOLD}-- Functional: confidence filter --{NC}")
try:
    from telegram_bot import format_message
    row = (19, "EURJPY", "SELL", 185.59200, 185.75410, 185.34884, 0.13508, 47.7, "test rationale", "2026-08-24 13:00:13")
    sizing = {"lots":0.01, "risk_amount":168.10, "below_min_lot":True, "currency":"JPY"}

    low_text = format_message(
        row, live_price=185.58600, drift_pips=0.6, live_rr=1.41,
        is_warn=False, is_suppress=False, is_untradeable=False, is_low_conf=True,
        technical_score=60, signal_health=45, confidence=55,
        bar_age_minutes=60, news_events=[], historical_stats=None, sizing=sizing
    )
    if "LOW CONFIDENCE" in low_text and "55% < 60%" in low_text and "DO NOT EXECUTE" in low_text:
        ok("Functional low-conf 55% -> ⛔ DO NOT EXECUTE")
    else:
        fail("Functional low-conf 55% banner", low_text[:300])

    high_text = format_message(
        row, live_price=185.58600, drift_pips=0.6, live_rr=1.41,
        is_warn=False, is_suppress=False, is_untradeable=False, is_low_conf=False,
        technical_score=75, signal_health=80, confidence=77,
        bar_age_minutes=60, news_events=[], historical_stats=None, sizing=sizing
    )
    if "EXECUTE: MARKET SELL NOW" in high_text and "77% confidence" in high_text:
        ok("Functional high-conf 77% -> 🚀 EXECUTE")
    else:
        fail("Functional high-conf 77% banner", high_text[:300])

except Exception as e:
    fail("Functional confidence test", str(e))
    import traceback
    traceback.print_exc()

# 6. Cron cleanliness (if available)
print(f"\n{BOLD}-- Cron cleanliness (server check) --{NC}")
try:
    import subprocess
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        warn("crontab not available", "skipping - run on server")
    else:
        cron_text = result.stdout
        # Count markers
        fx_starts = cron_text.count("fx-signal-pipeline")
        # Should be exactly 2 markers per block (start+end) = 2, but we have start and end same string? Actually marker and end marker both contain fx-signal-pipeline
        # Simpler: check for duplicate env vars leaked
        if "TELEGRAM_BOT_TOKEN=" in cron_text and "BASH_ENV" in cron_text:
            # If token appears in cron AND BASH_ENV exists, it's leaked from old install
            # Check if token line is NOT inside .env reference but as standalone env var
            lines = [l for l in cron_text.splitlines() if l.strip().startswith("TELEGRAM_BOT_TOKEN=")]
            if lines:
                fail("Cron should NOT contain TELEGRAM_BOT_TOKEN directly", "token leaked into crontab - reinstall with FORCE=1")
            else:
                ok("Cron does not leak TELEGRAM_BOT_TOKEN")
        else:
            ok("Cron does not leak secrets")

        if cron_text.count("# --- fx-signal-pipeline") > 1:
            fail("Cron has duplicate fx-signal-pipeline blocks", f"found {cron_text.count('# --- fx-signal-pipeline')} starts - clean with crontab -e")
        else:
            ok("Cron has single fx-signal-pipeline block")

        if "SIGNAL_MODE=relaxed" in cron_text:
            fail("Cron still forces SIGNAL_MODE=relaxed", "reinstall fx with FORCE=1")
        else:
            ok("Cron does not force relaxed")

        if "BASH_ENV=/opt/market/fx_signal/.env" in cron_text:
            ok("Cron FX uses BASH_ENV .env (pipeline mode)")
        else:
            warn("Cron FX pipeline not found", "is fx installed?")

        if "BASH_ENV=/opt/market/indices_signal/.env" in cron_text:
            ok("Cron Indices uses BASH_ENV .env")
        else:
            warn("Cron Indices pipeline not found")

except Exception as e:
    warn("Cron check skipped", str(e))

# 7. Existing preflights
print(f"\n{BOLD}-- Module preflights (quick) --{NC}")
try:
    # fx_config already validated
    # Check yahoo_client circuit
    sys.path.insert(0, str(ROOT))
    import yahoo_client
    info = yahoo_client.circuit_info()
    if info["open"]:
        warn(f"Yahoo circuit OPEN {info['remaining']:.0f}s", info["reason"])
    else:
        ok("Yahoo circuit closed")
except Exception as e:
    warn("Yahoo circuit check", str(e))

# Summary
print(f"\n{BOLD}=== SUMMARY ==={NC}")
print(f"{GREEN}PASS {len(PASS)}{NC}: {', '.join(PASS)}")
if WARN:
    print(f"{YELLOW}WARN {len(WARN)}{NC}: {', '.join(WARN)}")
if FAIL:
    print(f"{RED}FAIL {len(FAIL)}{NC}: {', '.join(FAIL)}")
    print(f"\n{RED}{BOLD}RESULT: {len(FAIL)} FAILURE(S) - fix above{NC}")
    sys.exit(1)
else:
    print(f"\n{GREEN}{BOLD}RESULT: ALL CHECKS PASS ✅ - strict + 60% filter working{NC}")
    print(f"{BOLD}Next signal should be [strict] and <60% will show ⛔ LOW CONFIDENCE{NC}")
    sys.exit(0)
