#!/usr/bin/env bash
# =============================================================================
# test_autosync.sh — exercises autosync.sh against throwaway local repos.
#
#   ./test_autosync.sh          # no network, no server, safe to run anywhere
#
# Creates a bare "origin" and two clones in a temp dir, then checks:
#   * local edits are committed and pushed (server → GitHub)
#   * remote commits are pulled in            (GitHub → server)
#   * a diverged branch rebases cleanly
#   * .gitignore is respected (.env never committed)
#   * a conflict pauses syncing instead of losing work
#   * a second run refuses to run while one is in flight
#   * --dry-run and --status change nothing
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSYNC="$REPO_ROOT/autosync.sh"
[[ -x "$AUTOSYNC" ]] || { echo "autosync.sh not executable" >&2; exit 1; }

WORK="$(mktemp -d)"
ORIGIN="$WORK/origin.git"
SERVER="$WORK/server"
SEED="$WORK/seed"
LOCK="$WORK/autosync.lock"

PASS=0
FAIL=0
GREEN='\033[92m'; RED='\033[91m'; BOLD='\033[1m'; NC='\033[0m'

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

ok()   { PASS=$((PASS + 1)); echo -e "${GREEN}  ok${NC}   $*"; }
bad()  { FAIL=$((FAIL + 1)); echo -e "${RED}  FAIL${NC} $*"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.test"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.test"
export GIT_CONFIG_NOSYSTEM=1

sync() {
    MARKET_REPO="$SERVER" AUTOSYNC_LOCK="$LOCK" AUTOSYNC_NET_TIMEOUT=20 \
        "$AUTOSYNC" "$@"
}

# Act as "another developer": always start from origin/main, commit one
# file, push. Keeps the fake upstream honest now that autosync pushes too.
seed_change() {
    local file="$1" content="$2" message="$3"
    git -C "$SEED" fetch -q origin main
    git -C "$SEED" reset -q --hard origin/main
    printf '%s\n' "$content" > "$SEED/$file"
    git -C "$SEED" add -A
    git -C "$SEED" commit -qm "$message"
    git -C "$SEED" push -q origin main
}

echo -e "${BOLD}setup${NC}"
git init -q --bare --initial-branch=main "$ORIGIN"
git clone -q "$ORIGIN" "$SEED" 2>/dev/null
git -C "$SEED" checkout -q -b main 2>/dev/null || true
printf 'hello\n' >"$SEED/app.txt"
printf '.env\n*.db\n' >"$SEED/.gitignore"
git -C "$SEED" add -A && git -C "$SEED" commit -qm "initial"
git -C "$SEED" push -q origin main
git clone -q "$ORIGIN" "$SERVER" 2>/dev/null
git -C "$SERVER" config user.name "test"
git -C "$SERVER" config user.email "test@example.test"

echo -e "${BOLD}status (before anything)${NC}"
sync --status >"$WORK/status.txt" 2>&1
check "status exits 0" "[[ \$? -eq 0 ]]"
check "status prints the branch" "grep -q 'branch.*main' '$WORK/status.txt'"

echo -e "${BOLD}server → GitHub${NC}"
printf 'server edit\n' >"$SERVER/server.txt"
printf 'SECRET=1\n' >"$SERVER/.env"
sync >/dev/null 2>&1
check "local change reached origin" \
    "git -C '$ORIGIN' log --oneline main | grep -q 'autosync'"
check "local change is on the remote branch" \
    "git -C '$ORIGIN' cat-file -e main:server.txt"
check ".env was NOT committed" \
    "! git -C '$ORIGIN' cat-file -e main:.env 2>/dev/null"
check "second run is a no-op" \
    "[[ \$(sync >/dev/null 2>&1; git -C '$SERVER' rev-list --count origin/main..main) -eq 0 ]]"

echo -e "${BOLD}GitHub → server${NC}"
seed_change up.txt "from upstream" "upstream change"
sync >/dev/null 2>&1
check "remote commit arrived on the server" "[[ -f '$SERVER/up.txt' ]]"

echo -e "${BOLD}divergence (both sides edited different files)${NC}"
printf 'local later\n' >"$SERVER/local2.txt"
seed_change remote2.txt "remote later" "remote later"
sync >/dev/null 2>&1
code=$?
check "sync exits 0 on a clean rebase" "[[ $code -eq 0 ]]"
check "local commit survived" "[[ -f '$SERVER/local2.txt' ]]"
check "remote commit survived" "[[ -f '$SERVER/remote2.txt' ]]"
check "history is linear (no merge commit)" \
    "[[ \$(git -C '$SERVER' rev-list --merges --count main) -eq 0 ]]"

echo -e "${BOLD}dry run${NC}"
printf 'pending\n' >"$SERVER/pending.txt"
before=$(git -C "$SERVER" rev-list --count main)
sync --dry-run >/dev/null 2>&1
check "dry-run did not commit" "[[ \$(git -C '$SERVER' rev-list --count main) -eq $before ]]"
check "dry-run left the file untracked" \
    "git -C '$SERVER' status --porcelain | grep -q 'pending.txt'"
rm -f "$SERVER/pending.txt"

echo -e "${BOLD}conflict handling${NC}"
seed_change shared.txt "version A" "shared remote"
sync >/dev/null 2>&1                       # bring the file down
printf 'version B\n' >"$SERVER/shared.txt"
seed_change shared.txt "version C" "shared remote 2"

sync >/dev/null 2>&1
check "conflict exits 4" "[[ \$? -eq 4 ]]"
check "conflict marker written" \
    "[[ -f '$SERVER/.git/market-autosync.conflict' ]]"
check "local commit kept (work is not lost)" \
    "git -C '$SERVER' log -1 --format=%s | grep -q 'autosync'"
check "server edit still in the working tree" \
    "grep -q 'version B' '$SERVER/shared.txt'"

sync >/dev/null 2>&1
check "later runs are paused (exit 5)" "[[ \$? -eq 5 ]]"

git -C "$SERVER" rebase --abort >/dev/null 2>&1 || true
git -C "$SERVER" fetch -q origin main
git -C "$SERVER" reset -q --hard origin/main
sync --clear-conflict >/dev/null 2>&1
check "clear-conflict re-enables syncing" \
    "[[ ! -f '$SERVER/.git/market-autosync.conflict' ]]"
sync >/dev/null 2>&1
check "sync works again" "[[ \$? -eq 0 ]]"

echo -e "${BOLD}single instance${NC}"
( flock 9; sleep 3 ) 9>"$LOCK" &
lock_pid=$!
sleep 0.3
out=$(MARKET_REPO="$SERVER" AUTOSYNC_LOCK="$LOCK" "$AUTOSYNC" 2>&1)
code=$?
wait "$lock_pid" 2>/dev/null
check "concurrent run skips cleanly" "[[ $code -eq 0 ]]"
check "concurrent run says so" "echo \"\$out\" | grep -qi 'in progress'"

echo -e "${BOLD}guards${NC}"
MARKET_REPO="$WORK/nonexistent" AUTOSYNC_LOCK="$LOCK" "$AUTOSYNC" >/dev/null 2>&1
check "non-repo path is rejected" "[[ \$? -eq 1 ]]"
git -C "$SERVER" checkout -q -b side-branch
sync >/dev/null 2>&1
check "wrong branch is rejected" "[[ \$? -eq 3 ]]"
git -C "$SERVER" checkout -q main

echo
if [[ "$FAIL" -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}all $PASS autosync checks passed${NC}"
    exit 0
fi
echo -e "${RED}${BOLD}$FAIL of $((PASS + FAIL)) autosync checks failed${NC}"
exit 1
