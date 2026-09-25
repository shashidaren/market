# HANDOFF — shashidaren/market

_Last updated 2026-09-25, right after PR #5 (insider movers tab) was merged and pulled on the server._
_Start here in any new session on this repo._

## 1. Current state

| Item | State |
|---|---|
| `main` on server | `7183433` (includes PR #5) |
| Server | `/opt/market`, logged in as **root**, **`sudo` is not installed** |
| Dashboard | systemd `market-dashboard` → `/usr/bin/python3 /opt/market/app.py`, port 5000, runs as root |
| Movers tab | code is deployed, but the restart failed (`sudo: command not found`) so `/movers` still returned 404 |
| Live Yahoo | `python3 insider_movers.py` on the server worked: 128 eligible stocks, 5/5 Yahoo requests OK |

First thing to do on the server:
```
cd /opt/market
systemctl restart market-dashboard
./check_all.sh        # expect: [ OK ] movers tab — http://localhost:5000/movers
```

## 2. Movers tab (PR #5): what it is

Shows the top 5 stocks by insider activity with their price action.

- `insider_movers.py`: ranking, dedupe, Yahoo price cache, metrics.
  CLI: `python3 insider_movers.py [--days 14] [--rank filings] [--limit 5] [--no-prices]`
- `app.py`: `GET /movers` (page) and `GET /api/movers?rank=score|filings|shares&days=7&limit=5` (limit capped at 10).
- `templates/movers.html`: plain-SVG candlestick charts with no JS libraries. Includes:
  - ▲/▼ insider markers on the trade date
  - filing-date dots
  - shaded insider window band
  - insider average-price lines
  - tooltips, 1M/3M/6M/1Y range buttons, trades table, 5-min auto refresh
- `test_insider_movers.py`: `python3 -m unittest test_insider_movers.py -v`. 25 tests, no network needed; the route tests need flask.
- `check_all.sh`: checks that `/movers` responds and that yfinance imports under `/usr/bin/python3`.

How it works:
- **Ranking**: stocks with `signal_score > 0` in the window (same rule as `/insider`).
  - Default order: score → # filings → gross shares.
  - Warrants (`WA`–`WE`) are skipped.
- **Dedupe**: one trade per (person, trade date, side, shares). S219 wins over S138 because it carries the price.
- **Prices**: `yf.Ticker(code.zfill(4)+".KL").history(period="1y", interval="1d", auto_adjust=False)`. These are raw prices, so they compare directly with the filed prices.
- **Cache** (in-process, shared by every browser):
  - TTL of 10 min in Bursa hours, 60 min otherwise.
  - Failures are negatively cached: 5 min for network errors, 30 min for no-data.
  - Stale data is served if a refresh fails.
  - A 429, or 3 empty replies in a row, pauses all fetches for `YAHOO_COOLDOWN_SECS` (default 900).
- **Env knobs**: `MOVERS_PRICE_TTL`, `MOVERS_PRICE_TTL_OFFHOURS`, `YAHOO_COOLDOWN_SECS` (see `.env.example`).
- **Verdict**: FRESH / LATE etc. reuse `announcements_signal/price_context._classify_move`, loaded by file path.
  - Never add `announcements_signal/` to `sys.path` from the root: its `collector.py` shadows the root `collector.py`.
- Flask caches templates (debug is off), so **restart the service after any template change**.

## 3. Movers: issues from the first live run (do these next)

```
 1 7079  TWL HOLDINGS    score 14  12 filings  net -299,312,874  RM0.025  +0.0%
 2 6114  MKH BERHAD      score 13  38 filings  net +317,068,494  RM1.99   +0.0%  FRESH
 3 5319  MKH OIL PALM    score 13  22 filings  net  +74,688,693  RM0.65   -1.5%  FRESH
 4 5243  VELESTO ENERGY  score 13  19 filings  net  -39,025,700  RM0.22   -4.3%
 5 5037  COMPUGATES      score 13  12 filings  net  +13,702,800  RM0.02  +33.3%  VERY LATE
```

1. **yfinance DeprecationWarning**:
   - Message: `'raise_errors' deprecated, do: yf.config.debug.hide_exceptions = False`.
   - Fix in `fetch_daily_bars()`: if `yf.config.debug.hide_exceptions` exists, set it to `False` and call `history()` without `raise_errors`. Otherwise keep `raise_errors=True` for older yfinance.
2. **Net shares are inflated by deemed-interest / group filings** (MKH: 38 filings, +317M).
   - Every holder of a deemed interest (holding company, directors, spouses) re-files the same transaction under a different name, so the per-person dedupe keeps all of them.
   - A flat price plus huge "buying" across one group probably means a take-over/offer or an internal restructuring, not a signal. Check the filings' `circumstances` to confirm.
   - Proposals:
     - Compute bought/sold from unique (trade_date, side, shares) across persons.
     - Add an `offer` method tag (take-over, takeover, offer, acceptance).
     - Flag or exclude offer-driven trades from the ranking.
3. **Penny stocks: one tick is a huge % move.**
   - 5037 at RM0.02: +33% is a single tick (0.015 → 0.02). 7079 trades at RM0.025.
   - Proposal: show "1 tick = x%" or flag prices under RM0.10.
4. **Hints mention `sudo`, but the server has none.** Drop `sudo` from the WARN text in `check_all.sh` and from the setup-error text in `insider_movers.py`.

## 4. Pre-existing problems (NOT caused by PR #5) seen in `check_all.sh`

1. **announcements_signal: the i_report filter is "unavailable", so Telegram insider alerts go out unfiltered (fail open).**
   - Error: `No module named 'app.providers'; 'app' is not a package`.
   - Root cause:
     - `i_report/app/` has **no `__init__.py`**, so Python treats it as a namespace package.
     - A regular module named `app` found anywhere on `sys.path` wins over a namespace package, even though `ireport_filter.py` puts `i_report/` first.
     - `announcements_config.py` adds the repo root to `sys.path`, and the root holds the dashboard's `app.py`.
     - Reproduced on `946a39f`, before PR #5.
   - Fix:
     - Add an empty `i_report/app/__init__.py`.
     - Check that `app.providers`, `app.analysis` and `app.engine` still import.
     - Rerun `announcements_signal/preflight_check.py`.
   - The preflight's hint (`pip install pandas-ta-classic`) is misleading here.
2. **gold-watcher: the service is inactive and has no Telegram token.**
   - `gold-watcher/config.py` reads `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` from `gold-watcher/.env`, which doesn't exist.
   - The central `/opt/market/.env` uses the key name `TELEGRAM_BOT_TOKEN`.
   - Fix: create `gold-watcher/.env`, or make `config.py` call `env_loader.load_env()` and fall back to `TELEGRAM_BOT_TOKEN`. Then run `systemctl restart gold-watcher`.
3. **`announcements_signal/telegram_bot.py` bugs:**
   - Line 341 reads `price_ctx['change_pct']`, but `price_context` returns `pct_move`.
     - The move therefore always reads 0.0.
     - The "stagnant price + insider selling" filter then suppresses **every net-selling alert**.
     - Fix: use `pct_move`.
   - Line 62: the log format `[%(levelname)]` should be `[%(levelname)s]`. Right now every log line turns into a "--- Logging error ---" traceback.
4. **The Yahoo circuit breaker is gone.**
   - PR #3's `yahoo_client.py` and `test_yahoo_client.py` (shared cooldown file `/tmp/market-yahoo-circuit.json`) are no longer on `main`, probably overwritten by PR #4.
   - `.env.example` still lists `YAHOO_CIRCUIT_PATH` and `FX_EMPTY_STREAK_TRIPS`.
   - The FX cron still runs every minute on the same IP. The movers tab has its own cooldown.

## 5. Movers tab constraints (for users)

- **Price feed**:
  - Yahoo via yfinance: unofficial, about 15 min delayed, daily bars only.
  - LEAP-market stocks and new listings are often missing; those cards show "no price data".
- **Rate limits**:
  - Yahoo limits are shared with the FX, gold, indices and Telegram price-context jobs on the same IP.
  - The tab makes at most 5 requests per TTL.
  - Top 5 by design (the API allows up to 10).
- **Insider data**:
  - Filings lag the trade by days.
  - S138 filings often have no price.
  - Deemed interests inflate net shares (see §3.2).
  - Method tags are best-effort guesses from the filing text.
  - Scores freeze once Telegram has delivered the alert.
- The first load after a restart takes a few seconds while prices download.
- For information only, not investment advice.

## 6. Workflow notes

- **Arena sessions**:
  - Each session works on its own `arena/<id>-market` branch.
  - Once its PR is merged, the session can no longer push.
  - Start a new session for follow-ups and point it at this file.
- **Arena sandbox access**:
  - The sandbox cannot reach Yahoo, Bursa or Google (only PyPI/npm).
  - Test there with a synthetic `news.db` and a fake price fetcher (see `test_insider_movers.py`).
  - Verify live data on the server with `python3 insider_movers.py`.
- **Server routine after pulling**:
  ```
  python3 -m py_compile app.py insider_movers.py
  python3 -m unittest test_insider_movers.py
  systemctl restart market-dashboard
  ./check_all.sh
  ```
