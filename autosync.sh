#!/usr/bin/env bash
# =============================================================================
# autosync.sh — keep the server checkout and GitHub in sync, both directions.
#
# One run does, in this order:
#   1. COMMIT  any local edits on the server (respecting .gitignore, so .env,
#              *.db and logs are never committed)
#   2. FETCH   origin
#   3. REBASE  local commits onto the remote branch (fast, linear history)
#   4. PUSH    the result back to origin
#
# That means: a fix pushed from Arena/PR reaches the server on the next tick,
# and a change you made directly on the server is backed up to GitHub on the
# next tick. The timer (deploy/market-autosync.timer) runs it every 5 minutes.
#
# Safety rules — this script will never do any of these:
#   * force-push                     (no --force, ever)
#   * commit ignored files           (git add -A obeys .gitignore)
#   * touch a repo mid-rebase/merge  (it refuses and says so)
#   * silently swallow a conflict    (it aborts the rebase, leaves your commit
#                                     intact, writes a marker and stops syncing
#                                     until a human looks at it)
#
# Usage:
#   ./autosync.sh                 # sync now (commit + pull + push)
#   ./autosync.sh --status        # show branch / dirty / ahead / behind only
#   ./autosync.sh --dry-run       # show what would happen, change nothing
#   ./autosync.sh --clear-conflict  # re-enable syncing after a manual fix
#
# Env:
#   MARKET_REPO            repo path (default: this script's directory)
#   AUTOSYNC_BRANCH        branch to sync (default: main)
#   AUTOSYNC_REMOTE        remote name (default: origin)
#   AUTOSYNC_NET_TIMEOUT   seconds per network operation (default: 120)
#   AUTOSYNC_LOCK          lock file (default: /tmp/market-autosync.lock)
#   AUTOSYNC_MERGE=1       use merge instead of rebase for a diverged branch
#
# Exit codes: 0 ok/skip · 1 usage/setup · 2 network/auth · 3 repo busy ·
#             4 conflict (syncing paused) · 5 paused by an earlier conflict
# =============================================================================

set -uo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO="${MARKET_REPO:-$(cd "$(dirname "$SCRIPT_PATH")" && pwd)}"
BRANCH="${AUTOSYNC_BRANCH:-main}"
REMOTE="${AUTOSYNC_REMOTE:-origin}"
NET_TIMEOUT="${AUTOSYNC_NET_TIMEOUT:-120}"
LOCK_FILE="${AUTOSYNC_LOCK:-/tmp/market-autosync.lock}"
MERGE_MODE="${AUTOSYNC_MERGE:-0}"

MODE="sync"
DRY_RUN=0
QUIET=0

GREEN='\033[92m'; YELLOW='\033[93m'; RED='\033[91m'; BOLD='\033[1m'; NC='\033[0m'
[[ -t 1 ]] || { GREEN=''; YELLOW=''; RED=''; BOLD=''; NC=''; }

usage() {
    sed -n '2,50p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --status)          MODE="status" ;;
        --dry-run|-n)      DRY_RUN=1 ;;
        --clear-conflict)  MODE="clear" ;;
        --quiet|-q)        QUIET=1 ;;
        --branch)          BRANCH="${2:?--branch needs a value}"; shift ;;
        --remote)          REMOTE="${2:?--remote needs a value}"; shift ;;
        -h|--help)         usage ;;
        *) echo "unknown option: $1 (try --help)" >&2; exit 1 ;;
    esac
    shift
done

log() {
    local level="$1"; shift
    local colour=""
    case "$level" in
        OK)    colour="$GREEN" ;;
        WARN)  colour="$YELLOW" ;;
        ERROR) colour="$RED" ;;
    esac
    [[ "$QUIET" == "1" && "$level" == "INFO" ]] && return 0
    printf '%s %s[%s]%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$colour" "$level" "$NC" "$*"
}

die() { log ERROR "$*"; exit "${2:-1}"; }

# ── preflight ───────────────────────────────────────────────────────
command -v git >/dev/null 2>&1 || die "git is not installed"

if [[ ! -d "$REPO/.git" && ! -f "$REPO/.git" ]]; then
    die "not a git checkout: $REPO (set MARKET_REPO=/opt/market)"
fi

gitc() { git -C "$REPO" "$@"; }

# `timeout` cannot call a shell function — network operations go through git
# directly, with a hard timeout so a hung fetch cannot block the timer.
git_net() { timeout "$NET_TIMEOUT" git -C "$REPO" "$@"; }

GIT_DIR_ABS="$(gitc rev-parse --absolute-git-dir 2>/dev/null || gitc rev-parse --git-dir 2>/dev/null)"
CONFLICT_MARKER="$GIT_DIR_ABS/market-autosync.conflict"

if [[ "$MODE" == "clear" ]]; then
    rm -f "$CONFLICT_MARKER"
    log OK "conflict marker cleared — syncing is enabled again"
    exit 0
fi

current_branch="$(gitc rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
full_repo="$(gitc rev-parse --show-toplevel 2>/dev/null || echo "$REPO")"

# ── status (read-only, no lock needed) ──────────────────────────────
if [[ "$MODE" == "status" ]]; then
    echo -e "${BOLD}autosync status${NC}  $full_repo"
    echo "  branch        : $current_branch (syncing '$BRANCH' on '$REMOTE')"
    echo "  remote        : $(gitc remote get-url "$REMOTE" 2>/dev/null || echo 'MISSING')"
    echo "  identity      : $(gitc config user.name 2>/dev/null || echo '<unset>') <$(gitc config user.email 2>/dev/null || echo '<unset>')>"
    dirty=$(gitc status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    echo "  local changes : $dirty file(s)"
    if gitc rev-parse --verify --quiet "refs/remotes/$REMOTE/$BRANCH" >/dev/null; then
        ahead=$(gitc rev-list --count "refs/remotes/$REMOTE/$BRANCH..HEAD")
        behind=$(gitc rev-list --count "HEAD..refs/remotes/$REMOTE/$BRANCH")
        echo "  vs $REMOTE/$BRANCH : ahead $ahead · behind $behind"
    else
        echo "  vs $REMOTE/$BRANCH : not fetched yet"
    fi
    [[ -f "$CONFLICT_MARKER" ]] \
        && echo -e "  ${RED}paused${NC}: unresolved sync conflict — fix, then ./autosync.sh --clear-conflict" \
        || echo "  state         : syncing"
    [[ -d "$GIT_DIR_ABS/rebase-merge" || -d "$GIT_DIR_ABS/rebase-apply" ]] \
        && echo -e "  ${YELLOW}rebase in progress${NC}"
    exit 0
fi

# ── single instance ─────────────────────────────────────────────────
if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE" || die "cannot write lock file $LOCK_FILE"
    if ! flock -n 9; then
        log INFO "another autosync run is in progress — skipping this tick"
        exit 0
    fi
fi

log INFO "repo $full_repo · branch $current_branch · target $REMOTE/$BRANCH"

[[ -f "$CONFLICT_MARKER" ]] && die \
    "syncing is paused after an unresolved conflict. Resolve it (git status), then run ./autosync.sh --clear-conflict" 5

[[ "$current_branch" == "$BRANCH" ]] || die \
    "checked-out branch is '$current_branch', but this timer syncs '$BRANCH'. Run: git -C \"$REPO\" switch $BRANCH" 3

for busy in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD; do
    [[ -e "$GIT_DIR_ABS/$busy" ]] && die \
        "a git $busy operation is in progress — finish or abort it first" 3
done

if [[ -f "$GIT_DIR_ABS/index.lock" ]]; then
    die "stale .git/index.lock exists; remove it if no git command is running" 3
fi

AUTOSYNC_COMMIT_NAME="market-autosync"
AUTOSYNC_COMMIT_EMAIL="autosync@$(hostname 2>/dev/null || echo server)"
commit_id() {
    gitc -c user.name="$AUTOSYNC_COMMIT_NAME" \
         -c user.email="$AUTOSYNC_COMMIT_EMAIL" "$@"
}

# ── 1. commit local server edits ────────────────────────────────────
changed=$(gitc status --porcelain --untracked-files=normal 2>/dev/null | wc -l | tr -d ' ')
if [[ "$changed" -gt 0 ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
        log INFO "[dry-run] would commit $changed local change(s):"
        gitc status --short | head -20 | sed 's/^/    /'
    else
        gitc add -A || die "git add failed"
        staged=$(gitc diff --cached --name-only | wc -l | tr -d ' ')
        if [[ "$staged" -gt 0 ]]; then
            files=$(gitc diff --cached --name-only | head -12 | tr '\n' ' ')
            commit_id commit -q \
                -m "autosync: server changes $(date '+%Y-%m-%d %H:%M')" \
                -m "Committed automatically by autosync.sh on $(hostname 2>/dev/null || echo server). Files: $files" \
                || die "git commit failed"
            log OK "committed $staged local change(s)"
        fi
    fi
else
    log INFO "no local changes to commit"
fi

# ── 2. fetch ────────────────────────────────────────────────────────
if [[ "$DRY_RUN" == "1" ]]; then
    log INFO "[dry-run] would fetch $REMOTE (timeout ${NET_TIMEOUT}s)"
else
    if ! git_net fetch --prune "$REMOTE" "$BRANCH"; then
        die "git fetch failed (network or credentials). Check: git -C \"$REPO\" remote -v && git -C \"$REPO\" ls-remote $REMOTE" 2
    fi
    log OK "fetched $REMOTE"
fi

REMOTE_REF="refs/remotes/$REMOTE/$BRANCH"
if ! gitc rev-parse --verify --quiet "$REMOTE_REF" >/dev/null; then
    die "remote branch '$BRANCH' not found on '$REMOTE' (check the branch name / remote)" 2
fi

behind=$(gitc rev-list --count "HEAD..$REMOTE_REF")
ahead=$(gitc rev-list --count "$REMOTE_REF..HEAD")
log INFO "vs $REMOTE/$BRANCH: ahead $ahead · behind $behind"

# ── 3. integrate remote changes ─────────────────────────────────────
if [[ "$behind" -gt 0 ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
        log INFO "[dry-run] would integrate $behind remote commit(s) via $([[ "$MERGE_MODE" == "1" ]] && echo merge || echo rebase)"
    else
        if [[ "$MERGE_MODE" == "1" ]]; then
            if ! gitc merge --no-edit "$REMOTE_REF" >/dev/null; then
                gitc merge --abort 2>/dev/null
                printf 'merge conflict with %s/%s at %s\n' "$REMOTE" "$BRANCH" "$(date '+%F %T')" >"$CONFLICT_MARKER"
                die "merge conflict — local commits were kept. Fix manually, then ./autosync.sh --clear-conflict" 4
            fi
            log OK "merged $behind remote commit(s)"
        else
            if ! commit_id rebase "$REMOTE_REF" >/dev/null; then
                gitc rebase --abort 2>/dev/null
                printf 'rebase conflict with %s/%s at %s\n' "$REMOTE" "$BRANCH" "$(date '+%F %T')" >"$CONFLICT_MARKER"
                die "rebase conflict — your local commit was kept, syncing paused. Resolve manually (git status), then ./autosync.sh --clear-conflict" 4
            fi
            log OK "rebased onto $behind remote commit(s)"
        fi
    fi
else
    log INFO "already up to date with $REMOTE/$BRANCH"
fi

# ── 4. push ─────────────────────────────────────────────────────────
ahead=$(gitc rev-list --count "$REMOTE_REF..HEAD")
if [[ "$ahead" -gt 0 ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
        log INFO "[dry-run] would push $ahead commit(s)"
    else
        if ! git_net push "$REMOTE" "HEAD:$BRANCH"; then
            die "git push failed (credentials / branch protection / non-fast-forward). No force-push is attempted." 2
        fi
        log OK "pushed $ahead commit(s) to $REMOTE/$BRANCH"
    fi
else
    log INFO "nothing to push"
fi

log OK "sync complete"
exit 0
