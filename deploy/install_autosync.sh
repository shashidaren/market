#!/usr/bin/env bash
# =============================================================================
# install_autosync.sh — enable the 5-minute GitHub <-> /opt/market autosync.
#
#   cd /opt/market && ./deploy/install_autosync.sh
#
# Prefers a systemd timer; falls back to cron on boxes without systemd.
# The server has no sudo — run this as root (you already are).
#
#   ./deploy/install_autosync.sh --cron        # force the cron variant
#   ./deploy/install_autosync.sh --uninstall   # remove timer/cron entry
#   ./deploy/install_autosync.sh --check       # show what is installed
#
# Before enabling, make sure the server can push:
#   git -C /opt/market remote -v
#   git -C /opt/market ls-remote origin        # must not ask for a password
# An SSH deploy key with write access is the usual setup.
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_DIR="/etc/systemd/system"
SERVICE="market-autosync.service"
TIMER="market-autosync.timer"
CRON_MARK="# market-autosync (managed by deploy/install_autosync.sh)"
LOG="/var/log/market-autosync.log"

MODE="install"
FORCE_CRON=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cron)      FORCE_CRON=1 ;;
        --uninstall) MODE="uninstall" ;;
        --check)     MODE="check" ;;
        -h|--help)   sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

GREEN='\033[92m'; YELLOW='\033[93m'; RED='\033[91m'; BOLD='\033[1m'; NC='\033[0m'
ok()    { echo -e "${GREEN}[ OK ]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; }

HAS_SYSTEMD=1
command -v systemctl >/dev/null 2>&1 || HAS_SYSTEMD=0
[[ "$FORCE_CRON" == "1" ]] && HAS_SYSTEMD=0

cron_installed() {
    command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -qF "$CRON_MARK"
}

show_status() {
    echo -e "${BOLD}autosync status${NC}  ($REPO)"
    if [[ "$HAS_SYSTEMD" == "1" ]]; then
        systemctl is-enabled "$TIMER" 2>/dev/null || true
        systemctl is-active "$TIMER" 2>/dev/null || true
        systemctl list-timers "$TIMER" --no-pager 2>/dev/null | head -3 || true
    elif cron_installed; then
        echo "cron: enabled"; crontab -l | grep -A1 -F "$CRON_MARK"
    else
        echo "cron: not installed"
    fi
    [[ -x "$REPO/autosync.sh" ]] && "$REPO/autosync.sh" --status || warn "$REPO/autosync.sh is missing or not executable"
}

case "$MODE" in
    check)
        show_status
        exit 0
        ;;
    uninstall)
        if [[ "$HAS_SYSTEMD" == "1" ]]; then
            systemctl disable --now "$TIMER" 2>/dev/null && ok "timer disabled"
            rm -f "$UNIT_DIR/$TIMER" "$UNIT_DIR/$SERVICE"
            systemctl daemon-reload
            ok "unit files removed"
        fi
        if cron_installed; then
            crontab -l 2>/dev/null | grep -vF "$CRON_MARK" \
                | grep -vF "$REPO/autosync.sh" | crontab -
            ok "cron entry removed"
        fi
        echo "autosync is off. Your repo is untouched."
        exit 0
        ;;
esac

# ── install ─────────────────────────────────────────────────────────
if [[ "$(id -u)" != "0" && "$HAS_SYSTEMD" == "1" ]]; then
    fail "run as root (the server has no sudo): ./deploy/install_autosync.sh"
    exit 1
fi

[[ -x "$REPO/autosync.sh" ]] || { fail "$REPO/autosync.sh is missing or not executable"; exit 1; }

if ! git -C "$REPO" remote get-url origin >/dev/null 2>&1; then
    fail "no 'origin' remote in $REPO — set one: git remote add origin <url>"
    exit 1
fi
if ! timeout 20 git -C "$REPO" ls-remote origin >/dev/null 2>&1; then
    warn "cannot reach origin yet. The timer will fail until credentials work:"
    warn "  git -C $REPO ls-remote origin"
    warn "  (an SSH deploy key with write access is the usual fix)"
fi

"$REPO/autosync.sh" --status

if [[ "$HAS_SYSTEMD" == "1" ]]; then
    # The unit files use /opt/market; rewrite them for this checkout so the
    # installer works from any path (e.g. a home-directory deploy).
    sed "s#/opt/market#$REPO#g" "$SCRIPT_DIR/$SERVICE" >"$UNIT_DIR/$SERVICE"
    sed "s#/opt/market#$REPO#g" "$SCRIPT_DIR/$TIMER"   >"$UNIT_DIR/$TIMER"
    chmod 644 "$UNIT_DIR/$SERVICE" "$UNIT_DIR/$TIMER"
    systemctl daemon-reload
    systemctl enable --now "$TIMER"
    ok "systemd timer enabled — every 5 minutes"
    echo
    systemctl list-timers "$TIMER" --no-pager 2>/dev/null | head -3
    echo
    echo "Logs:    journalctl -u $SERVICE -n 50 --no-pager"
    echo "Run now: systemctl start $SERVICE && journalctl -u $SERVICE -n 20 --no-pager"
    echo "Stop:    ./deploy/install_autosync.sh --uninstall"
else
    if ! command -v crontab >/dev/null 2>&1; then
        fail "neither systemd nor crontab is available; run ./autosync.sh from your own scheduler"
        exit 1
    fi
    touch "$LOG" 2>/dev/null || warn "cannot create $LOG (log will go to /dev/null)"
    { crontab -l 2>/dev/null | grep -vF "$CRON_MARK"; } | {
        cat
        echo "$CRON_MARK"
        echo "*/5 * * * * cd $REPO && ./autosync.sh >> $LOG 2>&1"
    } | crontab -
    ok "cron entry installed — every 5 minutes"
    echo "Logs: tail -f $LOG"
    echo "Stop: ./deploy/install_autosync.sh --uninstall"
fi

echo
warn "autosync commits + pushes server edits automatically. Review with: git -C $REPO log -5"
