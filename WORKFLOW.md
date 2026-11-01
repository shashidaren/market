# WORKFLOW.md — how this repo is managed

Short version: **the server is your dev box, GitHub is your backup, and
`autosync.sh` keeps the two in step without you thinking about it.**

Since 2026-11-01 the server runs a 5-minute timer that commits + pulls + pushes
automatically. Manual git is now the exception, not the routine.

---

## The two lines of history

```
GitHub main ................. your stable copy (what the server runs)
your server main ............ where you edit + test + run cron   ←─┐
arena/9332580f-market ....... branch where this session's work lives │
                                                                     │
autosync (every 5 min):  commit server edits → fetch → rebase → push ┘
```

- You normally work **directly on the server's `main`** (or edit the JSON the
  dashboard reads) and autosync backs it up to GitHub.
- Arena can only push to `arena/9332580f-market` (session tracking). Those
  commits are **not** on `main` until you merge them.
- Once the PR is merged, the server's next autosync tick pulls the changes in.
  No `git fetch && git merge` ritual on the box.

---

## A) Your everyday change (server)

```bash
cd /opt/market
# 1. edit files (code, or data/bursa_tickers.json for the company list)

# 2. sanity-test BEFORE the timer commits it
python3 tickers.py check
python3 -m py_compile app.py insider_movers.py top_mover.py tickers.py
python3 -m unittest test_tickers.py test_top_mover.py
# or the full sweep:
./check_all.sh

# 3. that's it — autosync commits + pushes within 5 minutes
./autosync.sh --status        # optional: see what is pending
./autosync.sh                 # optional: push right now instead of waiting
```

**Rules that keep you safe:**

- `.env`, `*.db`, `*.db.*` and logs are gitignored — secrets and trade data
  never get committed. Never `git add -f` them.
- Autosync commits *all* changed tracked files, so don't leave half-finished
  experiments in the tree when you walk away. `git revert <hash>` if one slips
  through.
- If autosync pauses on a conflict it says so; §D has the 4-line recovery.
- Keep each change small — a 5-minute commit cadence makes small commits
  automatic.

### Restart rules of thumb

| Changed | Action |
|---|---|
| Python the cron pipeline runs (`collector.py`, `scorer.py`, `fx_signal/*`, `announcements_signal/*`) | nothing — next cron run imports the new code |
| `app.py`, any `templates/*`, or `data/*.json` | `systemctl restart market-dashboard` (Flask caches templates and the registry is read at import) |
| `install_cron.sh` / any systemd unit | re-run the installer |

---

## B) Getting fixes from me (Arena)

When I finish a change you get a PR from `arena/9332580f-market`. Either:

**Merge the PR on GitHub** — then the server picks it up on the next autosync
tick. This is the normal path and needs nothing on the box.

Or, if you prefer the server to merge first:

```bash
cd /opt/market
git fetch origin
git merge origin/arena/9332580f-market
git push origin main          # or just let autosync push it
```

If both you and I edited the same lines, git will stop and tell you. Resolve,
`git add`, `git rebase --continue` (or `git merge --continue`), then let
autosync finish.

---

## C) Deploy / restart cheat-sheet

| Action | Command |
|---|---|
| Registry check | `python3 tickers.py check` |
| Registry gaps | `python3 tickers.py suggest --days 14` |
| Add a listing | `python3 tickers.py add --code 1234 --name "..." --sector Tech` |
| Full preflight | `./check_all.sh` |
| Dashboard status | `systemctl status market-dashboard` |
| Dashboard logs | `journalctl -u market-dashboard -n 80 --no-pager` |
| Top mover, live | `python3 top_mover.py` |
| Movers, live | `python3 insider_movers.py` |
| Yahoo breaker | `python3 yahoo_client.py` |
| Autosync status | `./autosync.sh --status` / `./deploy/install_autosync.sh --check` |
| Autosync logs | `journalctl -u market-autosync.service -n 50 --no-pager` |
| FX pipeline now | `cd fx_signal && python3 price_collector.py && python3 signal_engine.py && python3 outcome_tracker.py && python3 telegram_bot.py` |
| Watch FX logs | `tail -f /var/log/webscrap-fx-pipeline.log` |

### Gotchas

- **Market hours guard** (fx): the pipeline skips all of Saturday and Sunday
  before 21:00 UTC. A "stale bar" preflight WARN on the weekend is expected.
- **DB migration is automatic** — keep a `prices.db.bak` before big upgrades.
- **Rate limits** are shared: fx, indices, Telegram price-context, `/movers`
  and `/top` all use one Yahoo budget on one IP. `INTERVAL_MINUTES=5` for fx is
  still the recommendation.
- **Cron**: `crontab -l | grep -A3 "fx-signal-pipeline"` must show a job line.

---

## D) Autosync reference

```bash
cd /opt/market
./deploy/install_autosync.sh          # install the 5-minute systemd timer
./deploy/install_autosync.sh --check  # is it installed / running?
./deploy/install_autosync.sh --cron   # no systemd? use cron instead
./deploy/install_autosync.sh --uninstall

./autosync.sh --status                # branch, dirty files, ahead/behind, paused?
./autosync.sh --dry-run               # what would happen, changes nothing
./autosync.sh                         # sync now
./autosync.sh --clear-conflict        # re-enable after a manual fix
```

What a run does: **commit → fetch → rebase → push**. It never force-pushes,
never touches `.env`/`*.db`, refuses mid-rebase, and skips a tick if another
run is in flight.

### If autosync pauses on a conflict

```bash
cd /opt/market
git status                       # conflicted files
# fix them and keep the server change:
git add <files> && git rebase --continue
# ...or discard the server change and take GitHub's version:
git rebase --abort && git reset --hard origin/main
./autosync.sh --clear-conflict
./autosync.sh --status
```

`./check_all.sh` keeps warning about the conflict marker until it is cleared.

---

## E) If you've never run an installer (fresh box)

```bash
cd /opt/market
cp .env.example .env && chmod 600 .env && $EDITOR .env   # tokens + knobs
FORCE=1 INTERVAL_MINUTES=5 ./fx_signal/install_cron.sh
cd announcements_signal && ./install_cron.sh 2>/dev/null || true
./deploy/install_autosync.sh
./check_all.sh
```

### Speed of signal delivery (for context)

Closed-bar strategy → delivery latency is not critical:

```
cron tick (≤5 min) → collector (~30–80s, ~5s with FX_BATCH_FETCH=1)
→ signal_engine (<1s) → telegram_bot (~2–5s)
≈ up to ~5–6 min from bar close, dominated by Yahoo feed lag (1–3 min)
```

---

## One-line summary

**Edit on the server → autosync commits + pushes it. Merge my PR → autosync
pulls it. `main` is what runs.** If autosync is paused, fix it (§D) before
doing anything else, because that is the only thing now standing between your
server and GitHub.
