# HANDOFF — shashidaren/market

_Last updated 2026-11-01 (session `arena/9332580f-market`). Start here in any
new session on this repo._

Three things landed in this session:

1. **The Bursa ticker list is no longer a Python file** — one JSON record per
   company, aliases derived automatically (§2).
2. **A new dashboard tab, `/top`** — the single highest-probability mover
   (§3).
3. **`autosync.sh` + a 5-minute timer** — server edits are backed up to
   GitHub and GitHub changes reach the server, automatically (§4).

---

## 1. Current state

| Item | State |
|---|---|
| `main` on server | `895ea70` — **this session is not merged yet** |
| This session | branch `arena/9332580f-market` → PR (ticker registry, `/top` tab, autosync) |
| Server | `/opt/market`, logged in as **root**, **`sudo` is not installed** |
| Dashboard | systemd `market-dashboard` → `/usr/bin/python3 /opt/market/app.py`, port 5000, runs as root |
| Tabs | `/` news · `/insider` · `/movers` · `/top` (new) |
| gold-watcher | **RETIRED 2026-09-25** — see `gold-watcher/README.md` |

### First things to do on the server

```bash
cd /opt/market
git fetch origin
git merge origin/arena/9332580f-market     # this session's work
git push origin main

# 1. the registry must be valid before anything else
python3 tickers.py check                   # expect: [ OK ] registry is valid

# 2. tests (no network needed)
python3 -m py_compile app.py insider_movers.py yahoo_client.py top_mover.py tickers.py
python3 -m unittest test_tickers.py test_top_mover.py test_insider_movers.py test_yahoo_client.py
./test_autosync.sh                         # 24 checks in throwaway repos
cd announcements_signal && python3 -m unittest test_telegram_bot.py && cd ..

# 3. restart the dashboard so the new template/route load
#    (Flask caches templates; debug is off)
systemctl restart market-dashboard

# 4. health check
./check_all.sh
#   expect new lines: company registry · top mover tab · autosync timer

# 5. turn on autosync (needs the server to be able to push to GitHub)
git ls-remote origin >/dev/null && echo "push credentials OK"
./deploy/install_autosync.sh               # systemd timer, every 5 min
journalctl -u market-autosync.service -n 20 --no-pager   # first run
```

If the server cannot push (no deploy key / expired token), `install_autosync.sh`
still installs the timer but every run will log a fetch/push failure. Set up an
**SSH deploy key with write access** for the repo, or a credential helper —
autosync never asks for credentials itself.

---

## 2. Bursa ticker registry (the maintenance fix)

### The problem

`tickers_config.py` was an 822-line Python dict with **498 aliases for 205
companies**. One company needed four hand-typed lines, and forgetting one
silently sent that company's headlines to `noise` forever. Proof from the
migration: **"Hong Leong Bank" never matched** — the old file only had
`"5819"` for it. The registry now derives it from the company name.

### The new shape

```
data/bursa_tickers.json     one record per company
data/macro_keywords.json    theme keywords -> sector
tickers.py                  loader + matcher + validator + CLI
tickers_config.py           thin compatibility shim (same old dicts)
data/README.md              full maintenance guide
```

```json
{"code": "1155", "name": "Maybank", "legal_name": "Malayan Banking Bhd", "sector": "Banking"}
```

`derive_aliases()` produces `Maybank`, `Malayan Banking Bhd`,
`Malayan Banking`, `1155` from that single record. `Group`/`Holdings` are never
stripped (too ambiguous) and an alias claimed by two companies is ignored and
reported.

**Compatibility:** every one of the 498 legacy aliases still resolves (verified
during migration; the registry now yields 523 usable aliases, 395 of them
derived). `scorer.py` calls `registry.classify()`, and
`tickers_config.BURSA_TICKERS` / `MACRO_KEYWORDS` still exist in the old shape
for anything that imports them.

### Everyday use

```bash
python3 tickers.py check                      # validate + stats (exit 1 on errors)
python3 tickers.py suggest --days 7           # names in "noise" headlines we miss
python3 tickers.py match "Maybank, CIMB up on OPR hold"
python3 tickers.py add --code 8869 --name "Press Metal" \
    --legal-name "Press Metal Aluminium Holdings Bhd" --sector Materials
python3 tickers.py alias --code 8869 --add PMetal
python3 tickers.py list --search press
```

Details, schema table and the "when the world changes" checklist:
**`data/README.md`**.

`check_all.sh` now runs `tickers.py check --brief`, and warns if
`data/bursa_tickers.json` is not tracked by git (i.e. would not reach GitHub).

---

## 3. `/top` — single highest-probability mover

### What it is

`top_mover.py` + `templates/top.html` + `GET /api/top-mover`. It answers one
question: **among the stocks something is happening in, which single name is
most likely to move ≥N% next session?**

Unlike `/movers` (top 5 insider stocks + charts), `/top` shows one pick, a
gauge, the arithmetic behind the number, a chart and four runners-up.

### How the pick is made (all in `top_mover.py`)

1. **Universe (DB only, no Yahoo):** every stock the insider engine scored in
   `TOP_MOVER_INSIDER_DAYS` (default 10) ∪ every stock named in
   `scored_headlines` in `TOP_MOVER_NEWS_DAYS` (default 2). Parent + subsidiary
   names in one headline are collapsed to the most specific one.
2. **Shortlist:** ranked by `score×2 + filings + news×1.5 + bonus for both`,
   trimmed to `TOP_MOVER_SHORTLIST` (default 12, max 24). Every shortlisted
   stock is one Yahoo request per TTL — the same shared cache `/movers` uses.
3. **Probability:** the stock's own 1-year daily bars give the empirical
   frequency of a `THRESHOLD_PCT` (default 3%) day, smoothed with
   `(hits+1)/(n+20)`. That base rate is multiplied by hand-set condition
   factors — volume expansion (`vol_ratio_5d`, `vol_last_ratio`), volatility
   regime (`atr5 / 1y median ATR`), 52-week range position, news mentions,
   insider conviction and the move already in progress — clamped to
   0.6–4.0 and then to 1–95%.
4. **Confidence** is `high` (≥150 bars + news or insider), `medium` (≥60 bars)
   or `low`; below 30 bars the base rate is replaced by a 10% prior.

The UI prints `base rate × multiplier = probability` so the number can be
audited rather than believed. The disclaimer is on every payload and in the
tab's notes: **this is a heuristic ranking aid, not a calibrated model.**

### API

```
GET /api/top-mover?days=10&news_days=2&shortlist=12&runners=4
```

Returns `pick` (with `bars`), `runners` (without bars), `ranked`, `universe`
counts, `no_price` (what could not be priced and why), `price_feed`,
`threshold_pct`, `disclaimer`.

### Env knobs

`TOP_MOVER_SHORTLIST`, `TOP_MOVER_INSIDER_DAYS`, `TOP_MOVER_NEWS_DAYS`,
`TOP_MOVER_THRESHOLD_PCT` — see `.env.example`.

### Constraints (say these out loud before trusting it)

- Prices are Yahoo daily bars, delayed ~15 min — **not** tick data and not an
  official Bursa feed. LEAP/new listings are often missing; those names land
  in `no_price`.
- The Yahoo quota is shared with fx, indices, Telegram price-context and
  `/movers`. Raise `TOP_MOVER_SHORTLIST` only if the cooldown stays clear.
- News heat is only as good as the registry (§2): a missing company name never
  reaches the tab.
- The condition weights have **not** been back-tested. A follow-up worth doing:
  store each day's pick + outcome and calibrate against it (§7).

### CLI

```bash
python3 top_mover.py                       # live run, prints the pick + reasons
python3 top_mover.py --no-prices           # universe/shortlist only
python3 top_mover.py --json > /tmp/top.json
```

---

## 4. Autosync (server ↔ GitHub)

### What it does

`autosync.sh` runs every 5 minutes (`deploy/market-autosync.timer`) and does,
in order:

```
commit server edits → fetch origin → rebase onto origin/main → push
```

So a merged PR reaches `/opt/market` on the next tick, and an edit made
directly on the server is backed up to GitHub on the next tick. No more
"did I push that?".

### Safety

Never force-pushes, never commits `.env`/`*.db`/logs (`.gitignore`),
refuses to run mid-rebase/merge, refuses on the wrong branch, single-instance
via `flock`, 120 s network timeout. On a conflict it **aborts the rebase, keeps
your local commit**, writes `.git/market-autosync.conflict` and pauses
syncing (exit 4, then exit 5) until a human resolves it.

```bash
./autosync.sh --status              # branch, dirty files, ahead/behind, pause state
./autosync.sh --dry-run             # show what would happen
./autosync.sh                       # sync now
./autosync.sh --clear-conflict      # re-enable after resolving manually
```

### Install / verify / remove

```bash
./deploy/install_autosync.sh                 # systemd timer (falls back to cron)
./deploy/install_autosync.sh --check
./deploy/install_autosync.sh --cron          # force the cron variant
./deploy/install_autosync.sh --uninstall
journalctl -u market-autosync.service -n 50 --no-pager
```

`check_all.sh` reports the timer (or cron entry), a leftover conflict marker,
and a checkout that is not on `main`.

### Conflict recovery (the only manual bit)

```bash
cd /opt/market
git status                      # see the conflicted files
# edit them, then either:
git rebase --continue
# or throw the local server change away:
git rebase --abort && git reset --hard origin/main
./autosync.sh --clear-conflict
./autosync.sh --status
```

### Caveats

- Autosync commits **everything tracked and changed**, including edits you made
  for debugging. Review with `git log -5` occasionally; `git revert <hash>`
  works normally.
- Do not edit the same lines on the server and in a PR at the same time — that
  is exactly what produces a pause.
- The timer syncs **`main` only**. The Arena session branch is not tracked by
  the server; merge the PR first (or set `AUTOSYNC_BRANCH` deliberately).
- If `git push` needs credentials, the timer fails every 5 minutes. Set up a
  deploy key; check with `git ls-remote origin`.

---

## 5. Tests added this session

| Command | Covers |
|---|---|
| `python3 -m unittest test_tickers.py -v` | alias derivation, ambiguity, validation, CLI add/alias/check, `suggest`, shipped JSON integrity (20 tests) |
| `python3 -m unittest test_top_mover.py -v` | base-rate maths, probability bounds/monotonicity, universe merge, payload ranking, `no_price`, clamps, routes (19 tests) |
| `./test_autosync.sh` | both directions, divergence/rebase, `.gitignore`, conflict pause, lock, guards (24 checks, throwaway repos) |

Existing suites still pass: `test_insider_movers.py`, `test_yahoo_client.py`,
`announcements_signal/test_telegram_bot.py`.

The sandbox has no access to Yahoo/Bursa, so these use synthetic bars and a
fake cache. Verify live data on the server with `python3 top_mover.py`.

---

## 6. Previous session (2026-09-25) — still relevant

### Movers / insider pipeline

1. **yfinance DeprecationWarning** — `fetch_daily_bars()` sets
   `yf.config.debug.hide_exceptions = False` when the knob exists, else keeps
   `raise_errors=True`.
2. **Deemed-interest / group filings** — flow numbers count UNIQUE
   transactions, key `(trade_date, side, shares)`; per-person echoes stay in the
   trades table. Raw sums kept as `bought_all_persons` / `sold_all_persons`.
3. **`offer` method tag** — take-over/tender filings are tagged `offer`,
   excluded from flow numbers, and flagged with an amber OFFER badge when they
   dominate the window.
4. **Penny stocks** — `compute_metrics()` returns `tick_size`, `one_tick_pct`,
   `is_penny`; the tab shows a warning strip.
5. **`sudo` dropped** from `check_all.sh` WARN texts and setup errors (the
   server has none).

### Pre-existing problems fixed then

1. **i_report filter "unavailable"** — root cause was the missing
   `i_report/app/__init__.py`; the root `app.py` won the import. Fixed; the
   preflight now points at the real cause.
2. **gold-watcher disabled** (user decision — not used).
3. **`announcements_signal/telegram_bot.py`** — `[%(levelname)]` format bug, and
   the stagnant-price filter read the wrong key (`change_pct` instead of
   `pct_move`) which suppressed every net-selling alert. Missing price data now
   fails OPEN.
4. **Shared Yahoo circuit breaker** — `yahoo_client.py`; one state file
   (`YAHOO_CIRCUIT_PATH`, default `/tmp/market-yahoo-circuit.json`) shared by
   fx, indices, Telegram price-context and movers. Peek with
   `python3 yahoo_client.py`. Bursa no-data never trips it.

### Movers quick reference (unchanged)

- `insider_movers.py`: ranking, dedupe, Yahoo cache, metrics.
  `python3 insider_movers.py [--days 14] [--rank filings] [--limit 5] [--no-prices]`
- `app.py`: `GET /movers`, `GET /api/movers?rank=score|filings|shares&days=7&limit=5` (limit ≤ 10).
- `templates/movers.html`: plain-SVG candles, no JS libraries; 5-min refresh.
- Price cache: TTL 10 min in Bursa hours / 60 min otherwise; failures negatively
  cached; stale data served if a refresh fails.
- **Flask caches templates** — restart the service after any template change.
- Never add `announcements_signal/` to `sys.path` from the root: its
  `collector.py` shadows the root `collector.py`.

---

## 7. Known follow-ups (not started)

- **Calibrate `/top`.** Log each pick + the next session's actual move, then
  replace the hand-set multiplier weights with fitted ones. Until then the tab
  is a ranking aid. A first step is a daily cron writing
  `(date, code, probability, threshold, next_day_move)` to a small table.
- **Fill registry gaps.** `python3 tickers.py suggest --days 14` weekly; the
  current 35 code-less entries (EPF, FBM indices, foreign peers) are intentional.
- **Bursa official listing sync.** The JSON could be refreshed from Bursa's
  public listing if a stable endpoint exists; today it is curated by hand.
- FX cron still runs every minute on the shared IP; `INTERVAL_MINUTES=5` would
  be kinder to the Yahoo quota.
- Manual check of the MKH filings' `circumstances` (offer hypothesis, §6.3).
- `i_report/reports/` accumulates old .txt reports (cosmetic).

---

## 8. Workflow notes

- **Arena sessions**: each works on its own `arena/<id>-market` branch; once
  its PR is merged the session can no longer push. Start a new session for
  follow-ups and point it at this file.
- **Arena sandbox access**: only PyPI — no Yahoo, no Bursa, no Google. Test with
  synthetic DBs and fake fetchers (see `test_top_mover.py`), verify live data on
  the server.
- **Server routine after pulling**:
  ```bash
  python3 -m py_compile app.py insider_movers.py yahoo_client.py top_mover.py tickers.py
  python3 -m unittest test_tickers.py test_top_mover.py test_insider_movers.py test_yahoo_client.py
  systemctl restart market-dashboard
  ./check_all.sh
  ```
- **Branch/PR hygiene**: `WORKFLOW.md` describes the two lines of history and
  how autosync changes them.
