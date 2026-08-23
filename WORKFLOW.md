# WORKFLOW.md — how this repo is managed

Short version: **the server is your dev box, GitHub is your backup, and
my (Arena) fixes arrive via the `arena/01a029a1-market` branch. Your own
edits go straight to `main`.**

---

## The two lines of history

```
GitHub main ................. your stable copy (what the server runs)
your server main ............ where you edit + test + run cron
arena/01a029a1-market ....... branch where Arena session fixes land
```

- You only ever push to **`main`** — that's your workflow.
- Arena can only push to **`arena/01a029a1-market`** (session tracking).
  Those commits are **not** on `main` until you merge them.
- Both lines merge cleanly as long as you don't edit the same file I did
  in the same place (if git complains, resolve it and tell me).

---

## A) Your everyday manual change (server)

```bash
cd /opt/market
# 1. edit files

# 2. sanity-test BEFORE committing
python3 fx_signal/preflight_check.py        # or ./check_all.sh
python3 -m py_compile fx_signal/*.py        # catches syntax errors fast

# 3. commit + push (one topic per commit)
git add -A
git commit -m "short description of the change"
git push origin main
```

Done. GitHub now has a backup of your server state.

**Rules that keep you safe:**
- `git add -A` is fine: `.env`, `*.db`, `*.db.*` are gitignored — secrets
  and trade data never get committed.
- Never commit `.env` or `prices.db` manually (`git add -f` = footgun).
- Small commits, then `git revert <hash>` if something breaks.

---

## B) Getting fixes from me (Arena)

After I finish a change, run:

```bash
cd /opt/market
git fetch origin
git merge origin/arena/01a029a1-market      # bring my fixes into main
# test, then:
git push origin main
```

No reinstall needed for pure code fixes — the cron pipeline picks up new
code on the next run. If the change touched `install_cron.sh`, re-run it
(see D).

---

## C) Deploy / restart cheat-sheet (fx_signal)

| Action | Command |
|---|---|
| Full preflight | `python3 fx_signal/preflight_check.py` |
| Check pipeline is scheduled | `crontab -l \| grep -A3 "fx-signal-pipeline"` (must show a `*/5 * * * *` line) |
| (Re)install cron | `cd fx_signal && FORCE=1 INTERVAL_MINUTES=5 ./install_cron.sh` |
| Run pipeline now | `cd fx_signal && python3 price_collector.py && python3 signal_engine.py && python3 outcome_tracker.py && python3 telegram_bot.py` |
| Watch logs | `tail -f /var/log/webscrap-fx-pipeline.log` |
| Back up DB first | `cp fx_signal/prices.db fx_signal/prices.db.bak` |
| Check timestamps migrated | `sqlite3 fx_signal/prices.db "SELECT bar_time FROM price_signals WHERE timeframe='1h' ORDER BY bar_time DESC LIMIT 3;"` — expect space format `2026-08-23 21:00:00` (no `T`, no `+00:00`) |
| Latency probe | `cd fx_signal && python3 timing_probe.py` |

### Gotchas
- **Market hours guard**: the pipeline skips all of Saturday and Sunday
  before 21:00 UTC. A "stale bar" preflight WARN on the weekend is
  expected, not a failure.
- **DB migration is automatic** — `util.py` migrates legacy timestamps on
  the first run of any stage. Keep a `prices.db.bak` before big upgrades.
- **Rate limits**: the live crontab has a stray `* * * * *` FX pipeline
  line (every minute). That is fine *now* — the collector skip-if-fresh
  logic only hits Yahoo when the current closed 1h/4h bar is missing
  (~2 fetches per pair per hour, not 20 per minute). A 429 still trips a
  box-wide circuit (`/tmp/market-yahoo-circuit.json`, 15 min default)
  so gold-watcher / indices / announcements fail fast instead of
  extending the ban. Inspect / reset:
  `python3 /opt/market/yahoo_client.py` and `--reset`.
  Prefer `INTERVAL_MINUTES=5` if you re-run `install_cron.sh`, and
  delete the unmanaged `* * * * * run_pipeline.sh` line so you don't
  have two jobs. `FX_BATCH_FETCH=1` is the optional speed-up; it
  auto-falls back per-symbol if Yahoo returns bad data.

---

## D) If you've never run the installer (fresh box)

```bash
cd /opt/market/fx_signal
# tokens load automatically from an existing .env; otherwise pass them:
# TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... ./install_cron.sh
FORCE=1 INTERVAL_MINUTES=5 ./install_cron.sh
crontab -l | grep -A3 "fx-signal-pipeline"   # verify a job line exists
```

> Older versions of `install_cron.sh` wrote an EMPTY cron block (marker
> lines with no job). If you see that, run the command above — it replaces
> the block with a real `*/5 * * * * ... run_pipeline.sh` job and preserves
> the rest of your `.env`.

---

## E) Speed of signal delivery (for context)

Closed-bar strategy → delivery latency is not critical:

```
cron tick (≤5 min) → collector (~30–80s, ~5s with FX_BATCH_FETCH=1)
→ signal_engine (<1s) → telegram_bot (~2–5s)
≈ up to ~5–6 min from bar close, dominated by Yahoo feed lag (1–3 min)
```

---

## One-line summary

**Your edits → `git push origin main`. My fixes → `git merge
origin/arena/01a029a1-market` then `git push origin main`. Server is
always where it runs.**
