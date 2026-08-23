# indices_signal — outcome tracking (context)

## Added in PR #2 (merged to main)
- outcome_tracker.py: replays stored 4h price history to record TP/SL
  hits, marks EXPIRED, logs win-rate. Wired into run_pipeline.sh.
- signals_sent now stores sl/tp/atr (auto-migrated by
  price_collector.init_db / signal_engine.ensure_schema).
- telegram_bot: stop distance + 1-2% sizing rule + staleness note +
  Track record line (after >=10 resolved signals).
- indices_config: signal_expiry_hours, min_bars_for_stats.

## Bug on first run + fix (applied 2026-08-21)
First run on a DB with pre-existing signals crashed:
  TypeError: '<=' not supported between float and NoneType (resolve)
Cause: legacy signals logged before this feature have NULL sl/tp
  (migration added the columns but with NULL values).
Fix: get_open filters `sl IS NOT NULL`; resolve guards
  `if sl is None or tp is None: return None`; candle query ignores
  NULL high/low; expire_old also expires NULL-sl rows.
Verify: python3 indices_signal/outcome_tracker.py
  -> "Expired 2 unresolved signal(s) / No open outcomes" (the 2 are
     legacy EXPIRED, not real outcomes; TP rate 0% is expected).

## Deploy / verify
- git pull once on main; ensure outcome_tracker.py has the fix above.
- DB migrates automatically on next run.

## NOTE: there is NO "4H schedule" change
Live cron is `* * * * *` (every minute), identical to repo default.
Repo and live server are in sync at main. The earlier "e8c8a7f"
commit never existed.

## Yahoo rate-limit hardening (2026-08-23)
Live FX cron was 20 Yahoo `history()` calls *every minute* (10 pairs ×
2 timeframes). That is the smoking gun for `YFRateLimitError` / empty
frames. Fixes on this branch:

- `yahoo_client.py` — shared session + file-based circuit breaker
  (`/tmp/market-yahoo-circuit.json`, 15 min). One 429 opens the
  circuit for *every* module on the box.
- FX collector skip-if-fresh: if `prices.db` already has the current
  closed bar, do not call Yahoo. Every-minute cron stays useful for
  Telegram delivery; Yahoo is only hit after each bar close.
- Empty-streak trip: 3 consecutive empty replies in one collector run
  (Yahoo's silent ban signal) open the circuit. A single bad symbol
  does not.
- gold-watcher / indices / announcements / i_report all honour the
  same circuit.

Inspect: `python3 /opt/market/yahoo_client.py`
Reset:   `python3 /opt/market/yahoo_client.py --reset`

## indices_signal check (2026-08-23)

Live cron is **commented out** — the pipeline is not running. Do not
re-enable the old job unchanged: every 30 min it pulled 60d of 1h +
365d daily for GOLD/US30/US100 (6 fat Yahoo calls) and the hour-only
market filter treated Sunday afternoon as "US30 open".

Fixes on this branch:
- skip-if-fresh + incremental 10d catch-up once the DB is warm
- weekday-aware `is_market_open` (US closed Sat/Sun; gold closed Sat
  + Sunday before 22:00 UTC)
- timestamp migration to `YYYY-MM-DD HH:MM:SS` so same-day 4H TP/SL
  hits actually resolve (same class of bug as FX REVIEW #1)
- pipeline no longer `set -e`s past a failed Telegram send

To enable after merge (optional):
```
cd /opt/market/indices_signal && ./install_cron.sh
```
Default is every 30 minutes — fine now that Yahoo is skip-if-fresh.
