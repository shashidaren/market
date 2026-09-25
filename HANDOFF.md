# HANDOFF — shashidaren/market

_Last updated 2026-09-25 (session `arena/01a0d9a4-market`), after working through
the unfinished items below. Start here in any new session on this repo._

## 1. Current state

| Item | State |
|---|---|
| `main` on server | `7183433` (includes PR #5) — **this session's fixes are NOT pulled yet** |
| This session | branch `arena/01a0d9a4-market` → PR (movers fixes, telegram fixes, shared Yahoo breaker, gold-watcher retired) |
| Server | `/opt/market`, logged in as **root**, **`sudo` is not installed** |
| Dashboard | systemd `market-dashboard` → `/usr/bin/python3 /opt/market/app.py`, port 5000, runs as root |
| gold-watcher | **RETIRED 2026-09-25** (user decision: not used anymore) — see `gold-watcher/README.md` |

### First things to do on the server

```bash
cd /opt/market
git fetch origin
git merge origin/arena/01a0d9a4-market      # bring this session's fixes into main
# test, then push to main (see WORKFLOW.md §B)

# gold-watcher is retired — disable it (the repo no longer checks it):
systemctl disable --now gold-watcher
rm -f /etc/systemd/system/gold-watcher.service   # optional
systemctl daemon-reload

# deploy routine (no sudo anywhere — the server has none):
python3 -m py_compile app.py insider_movers.py yahoo_client.py
python3 -m unittest test_insider_movers.py test_yahoo_client.py
cd announcements_signal && python3 -m unittest test_telegram_bot.py && cd ..
systemctl restart market-dashboard
./check_all.sh        # expect: [ OK ] movers tab · [ OK ] gold-watcher — retired/inactive
                      # expect: [ OK ] i_report filter engine — loaded  (was FAIL, see §3.1)
```

## 2. What this session fixed (was "unfinished work")

### Movers (old §3)

1. **yfinance DeprecationWarning** — `fetch_daily_bars()` now sets
   `yf.config.debug.hide_exceptions = False` and calls `history()` without
   `raise_errors` when the knob exists; older yfinance keeps
   `raise_errors=True`. Regression-tested with a fake module.
2. **Net shares inflated by deemed-interest / group filings** (MKH: 38
   filings, +317M) — flow numbers (`bought`/`sold`/`net`/`gross`) now count
   UNIQUE transactions, key `(trade_date, side, shares)`, not per-person
   sums. The per-person echoes stay in the trades table; raw per-person
   sums are kept as `bought_all_persons` / `sold_all_persons`.
   **Still worth a manual look**: check the MKH filings' `circumstances`
   on Bursa to confirm it is a take-over/offer or internal restructuring.
3. **`offer` method tag + exclusion** — take-over / tender / acceptance
   filings are tagged `offer` and EXCLUDED from the flow numbers; a card
   where offer shares are ≥ ~20% of activity gets `offer_flag`, shown as
   an amber **OFFER** badge + "+N offer shares excl." note on the tab.
4. **Penny stocks** — `compute_metrics()` now returns `tick_size`
   (Bursa minimum bid: 0.005 / 0.01 / 0.02 / 0.10), `one_tick_pct` and
   `is_penny` (< RM0.10). The tab shows a warning strip, e.g.
   "⚠ penny stock (< RM0.10) — one RM0.005 tick ≈ 25.0%…".
5. **`sudo` dropped** from `check_all.sh` WARN texts and the
   `insider_movers.py` setup error (server has no sudo).

### Pre-existing problems (old §4)

1. **i_report filter "unavailable" (alerts failed open)** — root cause was
   the missing `i_report/app/__init__.py`: the dashboard's root `app.py`
   won the import and `app.providers` never resolved. The (empty)
   `__init__.py` is added and `app.providers` / `app.analysis` /
   `app.engine` now import correctly with `i_report/` first on `sys.path`.
   The preflight's misleading `pandas-ta-classic` hint was replaced: an
   app-shadowing error now points at `i_report/app/__init__.py`.
2. **gold-watcher: DISABLED instead of fixed** (user decision — not used).
   `check_all.sh` no longer runs its preflight; it now WARNs if the unit is
   ever active again. See `gold-watcher/README.md` for the disable
   commands and the "if you ever want it back" steps.
3. **`announcements_signal/telegram_bot.py` bugs** —
   - `[%(levelname)]` → `[%(levelname)s]` (no more "--- Logging error ---"
     traceback on every line);
   - the stagnant-price filter now reads `pct_move` (was `change_pct`,
     which is always missing → move always 0.0 → **every net-selling alert
     was suppressed**). Missing price data now fails OPEN (no Yahoo →
     alert still goes out), matching the i_report filter philosophy.
   - Regression tests: `announcements_signal/test_telegram_bot.py`.
4. **The shared Yahoo circuit breaker is back** — new root
   `yahoo_client.py` (tests: `test_yahoo_client.py`). All Yahoo jobs share
   one state file (`YAHOO_CIRCUIT_PATH`, default
   `/tmp/market-yahoo-circuit.json`, flock + atomic writes):
   - any rate-limit reply trips it for `YAHOO_COOLDOWN_SECS` (900 s);
   - `FX_EMPTY_STREAK_TRIPS` (3) consecutive empty replies from the
     **fx/indices** collectors trip it (their pairs are never legitimately
     empty);
   - **Bursa no-data is deliberately NOT counted** — LEAP/new listings are
     legitimately missing, so an open movers tab cannot pause the fx
     pipeline;
   - wired into: `fx_signal/price_collector.py` (skip fetch while open,
     record 429s/empties, keep old bars), `indices_signal/price_collector.py`
     (same), `announcements_signal/price_context.py` (alerts go out without
     a price block while open), and the movers `PriceCache` (honours a
     foreign trip; records its own rate-limit trips).
   - Peek at it any time: `python3 yahoo_client.py`.

## 3. Movers tab (PR #5) — unchanged parts, quick reference

- `insider_movers.py`: ranking, dedupe, Yahoo price cache, metrics.
  CLI: `python3 insider_movers.py [--days 14] [--rank filings] [--limit 5] [--no-prices]`
- `app.py`: `GET /movers` (page) and `GET /api/movers?rank=score|filings|shares&days=7&limit=5` (limit capped at 10).
- `templates/movers.html`: plain-SVG candlestick charts, no JS libraries.
  ▲/▼ insider markers, filing-date dots, insider window band, insider
  average-price lines, tooltips, 1M/3M/6M/1Y ranges, trades table,
  5-min auto refresh — plus the new OFFER badge and penny/tick strip.
- `test_insider_movers.py`: `python3 -m unittest test_insider_movers.py -v`
  (44 tests; the route tests need flask, one verdict test skips without
  yfinance).
- How ranking works: stocks with `signal_score > 0` in the window (same
  rule as `/insider`); warrants (`WA`–`WE`) skipped; default order
  score → # filings → gross shares; **flow numbers exclude offer-method
  trades and count each `(trade_date, side, shares)` once**.
- **Dedupe**: one trade per (person, trade date, side, shares) in the
  trades table; S219 wins over S138 because it carries the price.
- **Prices**: `yf.Ticker(code.zfill(4)+".KL").history(period="1y",
  interval="1d", auto_adjust=False)` — raw prices, comparable with filed
  prices.
- **Cache** (in-process, shared by every browser): TTL 10 min in Bursa
  hours / 60 min otherwise; failures negatively cached (5 min network,
  30 min no-data); stale data served if a refresh fails; a 429 or 3 empty
  replies in a row pauses fetches for `YAHOO_COOLDOWN_SECS` — and now
  also trips the shared breaker for the other jobs.
- **Env knobs**: `MOVERS_PRICE_TTL`, `MOVERS_PRICE_TTL_OFFHOURS`,
  `YAHOO_COOLDOWN_SECS`, `YAHOO_CIRCUIT_PATH`, `FX_EMPTY_STREAK_TRIPS`
  (see `.env.example`).
- **Verdict**: FRESH / LATE etc. reuse `announcements_signal/price_context._classify_move`,
  loaded by file path. Never add `announcements_signal/` to `sys.path`
  from the root: its `collector.py` shadows the root `collector.py`.
- Flask caches templates (debug is off), so **restart the service after
  any template change**.

## 4. Movers tab constraints (for users)

- **Price feed**: Yahoo via yfinance — unofficial, ~15 min delayed, daily
  bars only. LEAP-market stocks and new listings are often missing; those
  cards show "no price data" (and do NOT trip the shared breaker).
- **Rate limits**: Yahoo limits are shared with the FX, indices and
  Telegram price-context jobs on the same IP — the shared circuit breaker
  now covers all of them. The tab makes at most 5 requests per TTL; top 5
  by design (the API allows up to 10).
- **Insider data**: filings lag the trade by days; S138 filings often have
  no price; deemed-interest echoes inflate the per-person list (flow
  numbers now dedupe them — §2.2); method tags are best-effort guesses
  from the filing text; scores freeze once Telegram has delivered the
  alert. Two same-size trades on the same day by different people count
  once — accepted trade-off, re-filings are far more common.
- The first load after a restart takes a few seconds while prices download.
- For information only, not investment advice.

## 5. Known follow-ups (not started)

- The FX cron still runs every minute on the shared IP; with the shared
  breaker that is much safer, but `INTERVAL_MINUTES=5` would still be
  kinder to the Yahoo quota.
- Manual check of the MKH filings' `circumstances` (§2.2) to confirm the
  take-over/offer hypothesis.
- `i_report/reports/` accumulates old .txt reports (only cosmetic).

## 6. Workflow notes

- **Arena sessions**:
  - Each session works on its own `arena/<id>-market` branch.
  - Once its PR is merged, the session can no longer push.
  - Start a new session for follow-ups and point it at this file.
- **Arena sandbox access**:
  - The sandbox cannot reach Yahoo, Bursa or Google (only PyPI).
  - Test there with a synthetic `news.db` and a fake price fetcher (see
    `test_insider_movers.py`, `test_yahoo_client.py`,
    `announcements_signal/test_telegram_bot.py`).
  - Verify live data on the server with `python3 insider_movers.py`.
- **Server routine after pulling**:
  ```bash
  python3 -m py_compile app.py insider_movers.py yahoo_client.py
  python3 -m unittest test_insider_movers.py test_yahoo_client.py
  systemctl restart market-dashboard
  ./check_all.sh
  ```
