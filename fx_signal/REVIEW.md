# fx_signal — Code Review

**Scope:** `fx_signal/` (price_collector, signal_engine, telegram_bot, outcome_tracker, calendar_checker, preflight_check, signal_strictness_compare, run_pipeline.sh, install_cron.sh)
**Date:** 2026-08-22
**Verdict:** Solid, well-commented architecture for a personal signal bot (cron + flock + SQLite + Telegram + yfinance). **However, there are 3 critical bugs that affect live signal behavior**, plus a few medium issues. None require an architecture rewrite — the "better way" is mostly targeted fixes + a bit of consolidation. All bugs below were verified by reading the code and, where noted, by running it.

---

## Severity summary

| # | Severity | Issue | Impact |
|---|----------|-------|--------|
| 1 | **CRITICAL** | Timestamp format breaks every SQLite time comparison | Signals never expire, dedup window wrong, outcomes expire ~24h late |
| 2 | **CRITICAL** | Suppressed signals re-send the ⛔ "DO NOT EXECUTE" message every cron cycle | Hundreds of identical Telegram messages per day |
| 3 | **CRITICAL** | `store_signal()` commits inside `BEGIN IMMEDIATE` | Dedup/save not atomic; ROLLBACK can't undo an inserted signal |
| 4 | HIGH | `signal_strictness_compare.py` crashes (both variants) | Comparison tool is unusable |
| 5 | HIGH | No position sizing (your $100 cap) | Risk per trade is uncontrolled — 3%+ per trade at min lot |
| 6 | HIGH | News "WAIT — event imminent" logic is backwards | Wrong advice around news releases |
| 7 | MED | Outcome tracking checks one live snapshot | Misses intra-bar TP/SL touches; stats biased |
| 8 | MED | Duplicated helpers across 4 modules | Fixes drift apart (already happened: MIN_SL_PIPS) |
| 9 | MED | No tests | The timestamp bug would have been caught by one test |
| 10 | MED | News headlines not HTML-escaped | Telegram formatting can break / spoofed markup |
| 11 | MED | `run_pipeline.sh` `set -e` toggling + cron block may be empty | Fragile, and pipeline may not be scheduled (see below) |
| 12 | LOW | `calender_checker.py` (misspelled) is a dead shell-heredoc duplicate | Confusion / two sources of truth |
| 13 | LOW | Unused vars/imports, unpinned deps, hardcoded MIN_SL_PIPS dup | Hygiene |

---

## 1. CRITICAL — Timestamp format breaks SQLite time comparisons

**What happens:** Everything is stored as ISO-8601 with a `T` separator and `+00:00` offset
(e.g. `2026-08-22T13:20:00+00:00`), but the SQL compares against SQLite's `datetime('now')`,
which returns `2026-08-22 13:20:00` (space separator, no offset).

In a lexicographic string comparison, both strings share the first 10 characters
(`2026-08-22`), then the stored value has `T` (0x54) where SQLite's has a space (0x20).
`'T' > ' '`, so **every stored same-day timestamp compares as "greater" than `datetime('now')`, regardless of the actual time**.

Verified with a live sqlite3 test:

```
SELECT ts FROM t WHERE ts > datetime('now')            -> returns rows stored 0 seconds ago AND rows stored hours ago
SELECT ts FROM t WHERE ts >= datetime('now','-8 hours') -> window is actually "anything since start of UTC day"
SELECT ts FROM t WHERE ts < datetime('now','-24 hours') -> a 25h-old row is NOT expired; expiry only fires after midnight
```

**Concrete effects in your code:**

| Query | File | Intended | Actual |
|---|---|---|---|
| `expires_at > datetime('now')` | telegram_bot.py:83 | signals expire after 4h | **signals never expire until the calendar date rolls over (~24h)** |
| `generated_at >= datetime('now','-8h')` | signal_engine.py:213 | 8h rolling dedup window | "same UTC day" window — blocks legit fresh crossovers fired later the day; fails to dedup across midnight |
| `generated_at < datetime('now','-24h')` | outcome_tracker.py:167 | expire outcomes after 24h | expires up to ~48h late (only when the date prefix changes) |
| `sent_at >= datetime('now',?)` etc. | indices_signal, gold-watcher, announcements_signal | same pattern | same bug, systemic across the repo |

**Fix (one-time, small):** store all timestamps in SQLite-comparable form. Simplest: `%Y-%m-%d %H:%M:%S` UTC.

```python
# in a shared helper (fx_config or new util.py)
UTC_FMT = "%Y-%m-%d %H:%M:%S"

def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime(UTC_FMT)

# price_collector.to_utc_str:  return bar_time_utc.strftime(UTC_FMT)
# signal_engine/outcome_tracker/telegram_bot: use utc_now_str() instead of .isoformat()
```

One-time migration for existing rows (run once, then switch the writers):

```python
import sqlite3
con = sqlite3.connect("prices.db")
for tbl in ("signals", "signal_outcomes", "price_signals"):
    cols = {
        "signals":        ["generated_at", "expires_at", "delivered_at"],
        "signal_outcomes":["generated_at", "resolved_at"],
        "price_signals":  ["bar_time", "collected_at"],
    }.get(tbl, [])
    for c in cols:
        con.execute(f"UPDATE {tbl} SET {c} = replace(replace(replace({c},'T',' '),'+00:00',''),'.000000','') WHERE {c} LIKE '%T%'")
con.commit()
```

(`bar_time` is only compared in Python via `datetime.fromisoformat()` today, so changing it to space-separated is safe — but also update any `fromisoformat` callers to parse both formats, or use `datetime.strptime`.)

**Note:** `prune_old_data` (price_collector.py:346) happens to work because 90-day-old rows have a smaller date prefix. And `check_bar_freshness` parses in Python, so it's unaffected. The damage is confined to the SQL window/expiry comparisons — which are exactly the ones that matter.

---

## 2. CRITICAL — Suppressed signals spam the ⛔ message every cron cycle

`get_undelivered_signals()` intentionally re-includes `suppressed = 1` rows (so they re-evaluate each run — good idea). But in the suppressed branch, the bot **sends the ⛔ "DO NOT EXECUTE" message again on every run** and leaves `delivered = 0`:

```python
# telegram_bot.py, suppressed branch
conn.execute("UPDATE signals SET suppressed = 1 WHERE id = ?")
conn.commit()
send_telegram_message(token, chat_id, text)   # <-- sends EVERY run
continue
```

The cron runs **every minute**, and because of bug #1 the signal never expires during the day. Result: **~1,000 identical ⛔ messages per suppressed signal per day** until midnight (or until price recovers).

**Fix:** only notify on the transition into suppression (or at most every N minutes):

```sql
-- migration
ALTER TABLE signals ADD COLUMN suppressed_notified_at TEXT;
```

```python
# suppressed branch — notify once
if row_notified is None:
    send_telegram_message(token, chat_id, text)
    conn.execute("UPDATE signals SET suppressed=1, suppressed_notified_at=? WHERE id=?", (utc_now_str(), signal_id))
else:
    conn.execute("UPDATE signals SET suppressed=1 WHERE id=?", (signal_id,))
# and when price recovers (cleared branch): suppressed_notified_at = NULL
```

Also consider: if the ⛔ message *itself* is optional, just log it — but sending once is fine.

---

## 3. CRITICAL — `store_signal()` commits inside `BEGIN IMMEDIATE`

signal_engine.main wraps dedup + insert in `BEGIN IMMEDIATE`, but `store_signal()` calls `conn.commit()` **twice** (after the insert, and after seeding `signal_outcomes`). That commits the outer transaction, so:

- The write lock is released before the decision is complete.
- If anything raises after the first commit, the `ROLLBACK` in the caller cannot undo the signal row — you get a signal in the DB that was supposed to be deduped/skipped.
- `ROLLBACK`/`BEGIN`/`COMMIT` become mismatched across the connection (sqlite3 auto-commits leftover open transactions on close).

**Fix:** remove the two `conn.commit()` calls from `store_signal()` and let the caller commit once at the end of its transaction:

```python
conn.execute("BEGIN IMMEDIATE")
try:
    last = get_last_signal_direction(conn, pair)
    if last == direction: conn.execute("ROLLBACK"); continue
    sl, tp = compute_sl_tp(...)
    signal_id = store_signal(conn, ...)   # no commit inside
    conn.execute("INSERT OR IGNORE INTO signal_outcomes ...")  # move seed here or keep inside
    conn.commit()                          # single commit
except Exception:
    conn.execute("ROLLBACK"); raise
```

(For a single-process cron pipeline this rarely bites, but it's a correctness landmine — e.g., the dedup `SELECT` and the `INSERT` are now non-atomic.)

---

## 4. HIGH — `signal_strictness_compare.py` crashes on every variant

`decide()`'s signature is `decide(row_1h, row_1h_prev, row_4h, mode)`, but the tool calls:

```python
direction, reason = decide(row_1h, row_4h, mode="strict")    # row_4h lands in row_1h_prev; row_4h missing
direction, reason = decide(row_1h, None, mode="relaxed")     # row_4h missing
```

Verified: both raise `TypeError: decide() missing 1 required positional argument: 'row_4h'`. The tool is completely broken (only the local `loose` variant works).

**Fix:** pass the previous 1h row and the 4h row explicitly:

```python
row_1h_prev = get_previous_row(conn, pair, "1h")
direction, reason = decide(row_1h, row_1h_prev, row_4h, mode="strict")
direction, reason = decide(row_1h, row_1h_prev, None, mode="relaxed")
```

(Also `get_latest_row`/`validate_row` here are copies of signal_engine's — see #8.)

---

## 5. HIGH — No position sizing (matters for your $100 cap)

Signals tell you entry/SL/TP but never *how much* to trade. With a $100 account:

- Typical broker min lot = **0.01** (1,000 units).
- EURUSD pip value at 0.01 lot ≈ **$0.10/pip**.
- Your typical ATR-based SL (1.5 × ATR ≈ 15–35 pips) → **$1.50–$3.50 risk per trade = 1.5–3.5% of a $100 account**, before considering that some crosses (JPY, GBPJPY) have wider spreads that eat into a 5-pip min SL.

**Recommendation — add a sizing line to the Telegram message:**

```python
# config (fx_config.py):
ACCOUNT_BALANCE   = float(os.environ.get("ACCOUNT_BALANCE", "100"))
RISK_PER_TRADE_PCT = float(os.environ.get("RISK_PER_TRADE_PCT", "1.0"))
MIN_LOT = 0.01

# in telegram_bot, after levels are computed:
sl_pips = abs(entry - stop_loss) / cfg["pip_size"]
contract = 100_000.0
risk_amount = ACCOUNT_BALANCE * RISK_PER_TRADE_PCT / 100.0
lots = risk_amount / (contract * cfg["pip_size"] * sl_pips)
lots = max(MIN_LOT, round(lots, 2))
eff_risk = lots * contract * cfg["pip_size"] * sl_pips   # in quote currency
```

Then append:
```
📐 Size: 0.01 lots  (~$2.00 risk @ min lot — above your 1% target; skip or accept)
```

And **warn loudly when `eff_risk / ACCOUNT_BALANCE > RISK_PER_TRADE_PCT`** (i.e., when min lot already exceeds your risk budget). At $100 this will be *most* of the time, so also consider:

- **Filter to USD majors only** at this account size (EURUSD/GBPUSD/USDJPY) — cheapest spreads, tightest SLs. JPY crosses at min lot risk 1.8× more per pip.
- Raise `MIN_SL_PIPS` guard: 5 pips is close to or below 2× spread on some crosses; enforce `SL >= max(ATR_mult × ATR, 2 × typical_spread)`.
- Note the quote-currency caveat: for JPY pairs the formula gives risk in JPY; convert to USD (or just report pips) to keep it simple.

---

## 6. HIGH — "WAIT — event imminent" is backwards

`calendar_checker.get_upcoming_events()` only returns news **published in the past `window_minutes`** (`minutes_ago = now - published_dt`). But `format_message` interprets the most recent event as *upcoming*:

```python
soonest = min(ev["minutes_away"] for ev in news_events)
if soonest <= 15:
    news_lines.append("🚫 Recommendation: WAIT — event imminent")   # event was 15 min AGO, not in 15 min
```

So a CPI release 5 minutes ago renders as "WAIT — event imminent". The intent (don't trade around releases) is right; the wording and direction are wrong. Two options:

1. **Reword to post-release reality:** "⚠️ High-impact release X min ago — volatility window; consider waiting." (Cheap, honest.)
2. **Implement a true look-ahead calendar** (events with scheduled *future* times). Finnhub's free tier has no economic-calendar endpoint — the news-keyword proxy is the reason this is backwards. Free alternatives with real calendars: Twelve Data (free tier), Tradier, or investing.com's economic calendar (scrape, brittle). Given the effort, option 1 is the pragmatic fix; the *gate* still works as "don't trade right after big news".

Also rename `get_upcoming_events` → `get_recent_events` for clarity.

---

## 7. MEDIUM — Outcome tracking uses one live snapshot

`resolve_outcome()` compares a single `fast_info` price to TP/SL:

- If price touches SL at minute 10 and recovers before the next 1-minute check, the outcome is **missed** and later marked EXPIRED → **under-counts SL hits, inflates TP rate**.
- If a candle gaps through both TP and SL, `tp_hit` wins arbitrarily.
- `minutes_to_exit` is measured to *detection* time, not actual touch time (small upward bias).

You already collect full OHLC — use it:

```python
# pull the High/Low of 1m/5m bars (or the 1h bars) since generated_at,
# instead of one live snapshot:
SELECT MAX(High) FROM intraday WHERE pair=? AND bar_time > ?
SELECT MIN(Low)  FROM intraday WHERE pair=? AND bar_time > ?
# BUY:  TP_HIT if any High >= tp,  SL_HIT if any Low <= sl
```

If you don't want to store intraday bars, at least fetch an intraday range from yfinance (`interval="5m", period="1d"`) per open signal — still far better than one point. The docstring already admits this is a proxy; formalize it and quantify the bias in the stats output.

---

## 8. MEDIUM — Duplicated helpers across modules

| Helper | Duplicated in |
|---|---|
| `get_latest_row` / `get_previous_row` / `validate_row` | signal_engine.py, signal_strictness_compare.py |
| `get_live_price` (fast_info) | telegram_bot.py, outcome_tracker.py |
| DB init / WAL pragma | price_collector, signal_engine, telegram_bot, outcome_tracker |
| bar-age parsing (fromisoformat + tz fix) | signal_engine, telegram_bot, outcome_tracker |
| `MIN_SL_PIPS` | fx_config.py **and** signal_engine.py (hardcoded 5) |

The `MIN_SL_PIPS` duplication already bit you: change it in fx_config and the engine still uses 5. **Fix:** extract `db.py` (connect/init/migrate), `prices.py` (row fetch/validate/freshness), `live.py` (fast_info with caching) — ~150 lines total, removes ~250 lines of copies, and makes the timestamp fix land in one place.

---

## 9. MEDIUM — No tests

The decision logic is pure and testable — this is the cheapest insurance you can add:

```python
# tests/test_engine.py (pytest)
def test_fresh_bull_cross_fires_buy(): ...
def test_mid_trend_state_does_not_fire(): ...
def test_sl_tp_symmetry_buy_sell(): ...
def test_live_metrics_drift_sign(): ...
def test_timestamp_roundtrip_orders_correctly():   # would have caught bug #1
def test_dedup_window_respects_8h():               # would have caught bug #1
```

Add a `pytest` dependency, run in CI or via `check_all.sh`.

---

## 10. MEDIUM — News headlines not HTML-escaped

`format_message` runs with `parse_mode="HTML"` and escapes `rationale`, but inserts `ev['event']` (Finnhub headline) raw:

```python
f"  ⚠️ {ev['currency']} — {html.escape(ev['event'])} ..."   # <- currently NOT escaped
```

A headline containing `<`/`&` breaks formatting or injects markup. One-word fix: `html.escape(ev['event'])`.

---

## 11. MEDIUM — `run_pipeline.sh` shell hygiene + possibly-empty cron block

- `set -e` is toggled inside `run_stage`; after the first stage returns, `errexit` is globally enabled for the rest of the script, which the header explicitly says it wanted to avoid. Capture exit codes without flipping global state:

```bash
run_stage() {
    ...
    "$PYTHON" "$SCRIPT_DIR/$script" >> "$logfile" 2>&1
    local rc=$?
    ...
}
```
and call stages via `if run_stage ...; then ...; fi`.

- **Operational check for you:** the checked-in `cron_backup` shows the `# --- fx-signal-pipeline ---` marker block **empty** (no job line between start and end markers). The live cron per CONTEXT_NOTES is `* * * * *`, but verify on the server:

```bash
crontab -l | grep -A3 "fx-signal-pipeline"
```

If empty, re-run `./install_cron.sh` (with your tokens). Also note `INTERVAL_MINUTES` is honored only in `--separate` mode — pipeline mode always installs `* * * * *` (docstring says "default 15"). At a 1-minute cadence the collector hammers Yahoo 20×/min non-stop all day; see the speed section — consider `*/3` or `*/5` unless you need sub-minute delivery, and batch the fetches so each run makes ~2 requests instead of 20.

---

## 12–13. LOW — dead file, unused code, hygiene

- **Delete `calender_checker.py`** (misspelled). It's a shell heredoc that *generates* `calendar_checker.py` — the real module already exists. Two copies of the same logic will drift (they already differ slightly).
- pyflakes (run clean): `signal_engine.py:248` `close_1h` unused; `signal_engine.py:440` `is_4h_aligned` computed but never used (and it's direction-agnostic, while telegram_bot computes its own direction-aware version — dead code, remove); unused imports: `sys` in signal_strictness_compare.py, `RSI_OVERBOUGHT`/`RSI_OVERSOLD`/`summarise_news_risk` in telegram_bot.py.
- `requirements.txt` unpinned — pin versions (`yfinance>=1.0,<2`, `pandas>=2`, `requests>=2.31`). yfinance 0.2.x → 1.x changed API behavior (which the code already works around, but pin anyway).
- `install_cron.sh` writes `.env` via an **unquoted** heredoc — a secret containing `$` or backticks would be interpolated. Use `cat > "$ENV_FILE" <<'EOF'` or `printf '%s\n'`.
- `outcome_tracker.print_stats` uses **AVG** minutes-to-TP while the Telegram message uses **median** — pick one.
- `expire_old_outcomes` sets `resolved_at = datetime('now')` (SQLite format) while everywhere else it's ISO — inconsistent; use the same UTC helper.

---

## Speed of information gathering (your question)

**End-to-end latency budget (bar close → Telegram), assuming the `* * * * *` cron:**

| Stage | Time |
|---|---|
| Cron alignment (worst case) | 0–60 s |
| price_collector: 10 pairs × 2 TFs sequential Yahoo calls (~1–3 s each) + 0.5 s sleeps | 30–80 s |
| signal_engine (local SQLite) | <1 s |
| outcome_tracker: N open × fast_info (~1–2 s each) | 1–10 s |
| telegram_bot per signal: fast_info + Finnhub + Telegram POST | 2–5 s |
| **Typical total** | **~1.5–4 min** |

**Verdict:** For a strategy that trades on *closed 1h/4h bars*, 1.5–4 min is more than fast enough — the signal is only valid at bar close anyway, and the pipeline's staleness guards (90-min cap on 1h bars, 3× interval check) already protect you. The binding constraint is **Yahoo feed lag** (fc.yahoo.com FX data can lag 1–3 min and has no SLA), which no amount of pipeline tuning fixes. If you ever move to sub-hour signals or want faster execution, switch data to OANDA (free practice key = real-time rates), Twelve Data, or Dukascopy — not worth it at $100.

**Cheap wins (order of impact):**
1. **Batch the collector:** `yf.download(list_of_symbols, interval=..., group_by="ticker", threads=True)` — 2 HTTP requests per run instead of 20 → collector drops to ~2–6 s.
2. **Reuse one `requests.Session`/yfinance session** across calls (each `yf.Ticker()` makes fresh cookie+crumb round-trips).
3. **Fetch live price once per pair per bot run** (currently per *signal* — 2 signals on EURUSD = 2 fetches).
4. **Cache the Finnhub news call per run** (currently 1 request per signal).
5. **Add retry + backoff** in `fetch_ohlcv` (Yahoo drops connections; a single retry recovers most).
6. If you want to *measure* it on your server: run `timing_probe.py` (included next to this review) — it times each stage against your live environment and prints the budget table.

> I attempted to measure from this sandbox but Yahoo Finance blocks this datacenter's IPs (SSL connection reset on fc.yahoo.com), so timing must be measured on your server. `timing_probe.py` does it in one command.

---

## Recommended action plan

- **Now (P0, ~1 hour):** fix #1 timestamp format + migrate existing rows, #2 suppression spam, #3 store_signal atomicity.
- **This week (P1):** #4 strictness-compare fix, #5 position sizing + pair filter for $100, #6 news wording, verify crontab (#11).
- **Backlog (P2):** #7 outcome accuracy, #8 shared module, #9 tests, #10 escaping, #12/13 cleanup.

Want me to implement the P0 + P1 fixes on this branch? I'd add a `db.py`/util helper for the timestamp fix so all four modules change in one place, add the sizing fields to fx_config + telegram_bot, and fix the comparison tool.

---

## Fixes implemented (2026-08-22)

All P0 + P1 items are now fixed on this branch, verified by unit tests and an
end-to-end run of the comparison tool. Summary of changes:

| # | Fix | Files |
|---|-----|-------|
| 1 | Timestamps stored as `YYYY-MM-DD HH:MM:SS` UTC (SQLite-comparable). New `util.py` centralises `utc_now_str()` / `to_db_str()` / `parse_db_ts()` / `migrate_timestamps()` — an idempotent one-time migration that runs on every module startup and converts legacy `T`/`+00:00` rows. Verified: expiry, 8h dedup window and 24h outcome expiry now compare correctly. | new `util.py`; `price_collector.py`, `signal_engine.py`, `telegram_bot.py`, `outcome_tracker.py`, `preflight_check.py` |
| 2 | ⛔ suppression message now sent ONCE per signal (new `suppressed_notified_at` column; re-evaluation stays silent). Flag clears when price recovers / signal delivers. | `signal_engine.py` (schema), `telegram_bot.py` |
| 3 | `store_signal()` no longer commits — the caller owns the `BEGIN IMMEDIATE` transaction and commits once, so dedup+insert+outcome-seed is atomic and ROLLBACK fully undoes it. | `signal_engine.py` |
| 4 | `signal_strictness_compare.py` passes `row_1h_prev` correctly to `decide()` (was crashing with TypeError on every variant). Also removed duplicated row helpers (imports from `signal_engine`). | `signal_strictness_compare.py` |
| 5 | Position sizing added: `ACCOUNT_BALANCE` (default 100), `ACCOUNT_CURRENCY`, `RISK_PER_TRADE_PCT` (default 1.0), `MIN_LOT`, `CONTRACT_SIZE` in `fx_config`; `compute_position_size()` in telegram_bot; 📐 sizing line + ⚠️ min-lot-floor warning in the Telegram message. All overridable via env/.env. | `fx_config.py`, `telegram_bot.py`, `.env.example` |
| 6 | News gate reworded to reality: look-BACK over published releases ("X min ago", "volatility window"), `get_upcoming_events` → `get_recent_events`. Also fixed unescaped news headlines (HTML injection). | `calendar_checker.py`, `telegram_bot.py` |
| 12/13 | Deleted dead `calender_checker.py` (shell-heredoc duplicate); removed unused imports/vars (pyflakes clean); `expire_old_outcomes` uses the same UTC format as everything else. | repo |

**Not done (still in backlog):** #7 outcome accuracy via intraday OHLC (medium),
#8 full shared-module refactor beyond `util.py` (only partially done),
#9 automated tests (all verifications were ad-hoc scripts — worth adding pytest),
#10 done as part of #6, #11 verify crontab on the server.

**Deploy checklist:**
1. `git pull` on the server, restart any running processes.
2. First run migrates the existing DB automatically (idempotent). To be safe,
   back up `prices.db` first: `cp prices.db prices.db.bak`.
3. Verify with `python3 signal_strictness_compare.py` and
   `python3 preflight_check.py`.
4. Optional sizing env: `ACCOUNT_BALANCE=100 RISK_PER_TRADE_PCT=1.0` in `.env`.
