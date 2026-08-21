#!/bin/bash
# =============================================================================
# check_all.sh — full system health check for the market signal stack.
#
# Runs every module's preflight, checks systemd services, cron entries,
# and the dashboard. Safe to run any time (read-only by default).
#
# Usage:
#   ./check_all.sh               # read-only checks
#   ./check_all.sh --send-test   # also sends ONE test message per Telegram
#                                # module (fx, indices, gold, announcements)
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

SEND_TEST=""
[[ "${1:-}" == "--send-test" ]] && SEND_TEST="--send-test"

PASS=()
FAIL=()
WARN=()

GREEN='\033[92m'; RED='\033[91m'; YELLOW='\033[93m'; BOLD='\033[1m'; NC='\033[0m'

banner() {
    echo
    echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD} $1${NC}"
    echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
}

run_preflight() {
    local name="$1" dir="$2" extra="${3:-}"
    banner "$name"
    if ( cd "$dir" && python3 preflight_check.py $extra ); then
        PASS+=("$name")
    else
        FAIL+=("$name")
    fi
}

# ── 1. Module preflights ────────────────────────────────────
run_preflight "fx_signal"            "fx_signal"            "$SEND_TEST"
run_preflight "indices_signal"       "indices_signal"       "$SEND_TEST"
run_preflight "gold-watcher"         "gold-watcher"         "$SEND_TEST"
run_preflight "announcements_signal" "announcements_signal" "$SEND_TEST"

# ── 2. Root collector / scorer (news pipeline) ──────────────
banner "collector / scorer (news.db)"
python3 - <<'EOF'
import sqlite3, sys
from datetime import datetime, timedelta, timezone
try:
    con = sqlite3.connect("news.db")
    raw = con.execute("SELECT COUNT(*), MAX(collected_at) FROM raw_headlines").fetchone()
    print(f"[ OK ] raw_headlines — {raw[0]} rows, last collected {raw[1]}")
    scored = con.execute("SELECT COUNT(*) FROM raw_headlines").fetchone()[0] \
           - con.execute("SELECT COUNT(*) FROM scored_headlines").fetchone()[0]
    print(f"[ OK ] scorer backlog — {scored} unscored headline(s)")
    if raw[1]:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(raw[1])
        hours = age.total_seconds() / 3600
        if hours > 1.5:
            print(f"[WARN] last collection was {hours:.1f}h ago — is the */30 cron running?")
            sys.exit(2)
    sys.exit(0)
except Exception as e:
    print(f"[FAIL] news.db check: {e}")
    sys.exit(1)
EOF
case $? in
    0) PASS+=("collector/scorer") ;;
    2) WARN+=("collector/scorer (stale — check cron)") ;;
    *) FAIL+=("collector/scorer") ;;
esac

# ── 3. systemd services ─────────────────────────────────────
banner "systemd services"
if command -v systemctl >/dev/null 2>&1; then
    for svc in gold-watcher market-dashboard; do
        state=$(systemctl is-active "$svc" 2>/dev/null)
        if [[ "$state" == "active" ]]; then
            echo "[ OK ] $svc — active"
            PASS+=("$svc.service")
        elif systemctl list-unit-files 2>/dev/null | grep -q "^$svc"; then
            echo "[FAIL] $svc — $state (systemctl restart $svc)"
            FAIL+=("$svc.service")
        else
            echo "[WARN] $svc — not installed (cp $svc.service /etc/systemd/system/)"
            WARN+=("$svc.service not installed")
        fi
    done
else
    echo "[WARN] systemctl not available (not on the server?)"
    WARN+=("systemd checks skipped")
fi

# ── 4. Cron entries ─────────────────────────────────────────
banner "cron entries"
if command -v crontab >/dev/null 2>&1 && crontab -l >/dev/null 2>&1; then
    CRON=$(crontab -l 2>/dev/null | grep -v "^#")
    check_cron() {
        local label="$1" pattern="$2" optional="${3:-}"
        if echo "$CRON" | grep -q "$pattern"; then
            echo "[ OK ] cron: $label"
            PASS+=("cron: $label")
        elif [[ -n "$optional" ]]; then
            echo "[WARN] cron: $label — not enabled (optional)"
            WARN+=("cron: $label disabled")
        else
            echo "[FAIL] cron: $label — MISSING"
            FAIL+=("cron: $label")
        fi
    }
    check_cron "news collector"        "collector.py"
    check_cron "news scorer"           "scorer.py"
    check_cron "bursa pipeline"        "announcements_signal"
    check_cron "fx pipeline"           "fx_signal/run_pipeline.sh"
    check_cron "indices pipeline"      "indices_signal/run_pipeline.sh" optional
else
    echo "[WARN] no crontab for this user"
    WARN+=("cron checks skipped")
fi

# ── 5. Dashboard HTTP ───────────────────────────────────────
banner "dashboard (port 5000)"
if command -v curl >/dev/null 2>&1; then
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://localhost:5000/api/stats 2>/dev/null)
    if [[ "$code" == "200" ]]; then
        echo "[ OK ] dashboard responding — http://localhost:5000"
        PASS+=("dashboard")
    else
        echo "[FAIL] dashboard not responding on :5000 (HTTP ${code:-none})"
        FAIL+=("dashboard")
    fi
fi

# ── Summary ─────────────────────────────────────────────────
banner "SUMMARY"
echo -e "${GREEN}PASS (${#PASS[@]}):${NC} ${PASS[*]}"
[[ ${#WARN[@]} -gt 0 ]] && echo -e "${YELLOW}WARN (${#WARN[@]}):${NC} ${WARN[*]}"
if [[ ${#FAIL[@]} -gt 0 ]]; then
    echo -e "${RED}FAIL (${#FAIL[@]}):${NC} ${FAIL[*]}"
    echo
    echo -e "${RED}${BOLD}RESULT: ${#FAIL[@]} FAILURE(S) — see sections above${NC}"
    exit 1
else
    echo
    echo -e "${GREEN}${BOLD}RESULT: ALL SYSTEMS GO ✅${NC}"
    exit 0
fi
