# Bursa announcements collector

`collector.py` fetches the latest company announcements from Bursa Malaysia.
The API is behind a Cloudflare **managed JS challenge** (`403`,
`cf-mitigated: challenge`, “Just a moment…”). `cloudscraper==1.2.71` is the
latest PyPI release and solves that challenge only intermittently; there is no
upgrade path. This module makes the collector resilient and makes failure
visible without changing the rest of the pipeline.

## What changed

- **Page 1 first.** The latest 20 rows are the routine requirement. Page 1 is
  retried with a **fresh cloudscraper instance** per attempt and 1–3 s jitter.
  Pages 2–3 are best-effort catch-up only after page 1 succeeds.
- **Bounded request budget.** Defaults: `RETRY_MAX=5` page-1 attempts,
  `ANNOUNCEMENTS_MAX_REQUESTS=5` per run, and a shared rolling
  `ANNOUNCEMENTS_HOURLY_MAX_REQUESTS=20`. The old cron used 3 requests/run
  (18/hour); 20/hour is the hard ceiling. Lower values are allowed; higher
  hourly values are clamped to 20.
- **Heartbeat.** `news.db.collector.json` records `last_success_at` after a run
  that fetched *and stored* page 1, even if it contained zero new rows. It also
  records the measured last-run outcome and the rolling request budget. No
  existing SQLite table is rewritten.
- **Visible failure.** Exit `0` only when page 1 succeeded (or another run holds
  the lock). Exit `1` when page 1 failed; exit `2` for bad configuration. Cron
  and `check_all.sh` now see the failure.
- **Log hygiene.** One line per failure with page/status/attempt/reason. Repeated
  identical failures are capped per run; `DEBUG=1` enables the first traceback
  per failure kind.
- **Preflight.** `preflight_check.py` judges health from the heartbeat, prints
  the cloudscraper version and measured last-run outcome, and keeps
  `published_date` counts as secondary information. Weekends/Bursa holidays
  explain old publications; they do not excuse a dead collector, because cron
  runs every day and a successful empty fetch still refreshes the heartbeat.

## Operating knobs

See `.env.example`. The important ones:

| Env | Default | Meaning |
|---|---:|---|
| `RETRY_MAX` | 5 | Total page-1 attempts, including the first try |
| `ANNOUNCEMENTS_MAX_REQUESTS` | 5 | API requests per run |
| `ANNOUNCEMENTS_HOURLY_MAX_REQUESTS` | 20 | Shared rolling hourly cap (hard max 20) |
| `ANNOUNCEMENTS_JITTER_MIN` / `_MAX` | 1 / 3 | Seconds between page-1 retries |
| `ANNOUNCEMENTS_STALE_MINUTES` | 60 | Heartbeat age that fails preflight |
| `DEBUG` | 0 | Tracebacks for collector failures |
| `BURSA_HOLIDAYS` | empty | Extra `YYYY-MM-DD` closures, comma-separated |
| `ANNOUNCEMENTS_BROWSER_SOLVER` | 0 | Opt-in Chromium challenge solver |

`bursa_holidays.json` contains shipped XKLS non-trading weekdays. Verify it
against the official Bursa trading calendar each year; ad-hoc closures can be
added through `BURSA_HOLIDAYS` without a code change.

## When to enable the browser solver

Measure cloudscraper’s per-request success over 20 requests (`x/20`):

- **≥ 0.5:** retries alone are the answer. Keep the browser off.
- **0.2–0.5:** retries are primary; enable the browser only if the heartbeat
  still goes stale or page-1 run success is unacceptably low.
- **≈ 0:** cloudscraper cannot solve the challenge; the browser path is the
  realistic fallback, but it remains opt-in and must be installed/verified on
  the server.

At the observed ~1/3 per-request success rate, five independent page-1 attempts
give about `1 - (2/3)^5 ≈ 87%` page-1 success before the hourly cap. The shared
cap trades a little catch-up for staying near the old request ceiling.

Enable only after installing dependencies:

```bash
pip install selenium
# Install a matching Chromium + chromedriver pair, e.g. on Debian/Ubuntu:
# apt-get install chromium chromium-driver
ANNOUNCEMENTS_BROWSER_SOLVER=1 python3 collector.py
```

Optional explicit paths:

```bash
CHROMIUM_BINARY=/usr/bin/chromium
CHROMEDRIVER_BINARY=/usr/bin/chromedriver
```

The solver navigates to the API once, waits for `cf_clearance`, then reuses the
cookie and browser User-Agent with fresh cloudscraper instances for pages 1–3.
If Selenium/Chromium/chromedriver is missing, the budget is exhausted, or the
challenge does not clear, it logs one line and falls back to cloudscraper. It
never crashes the pipeline. Clearance cookies can be IP/User-Agent/TLS bound,
so verify live behaviour on the server before relying on this path.

## Verify

Offline tests (no Bursa access required):

```bash
cd announcements_signal
python3 -m unittest test_collector.py test_preflight_check.py test_browser_solver.py -v
```

On the server:

```bash
cd /opt/market/announcements_signal
python3 collector.py; echo "exit=$?"
python3 preflight_check.py
cat ../news.db.collector.json
```

Expected healthy heartbeat fields:

```json
{
  "last_success_at": "2026-11-01T03:40:00+00:00",
  "last_run": {
    "outcome": "success",
    "page1_ok": true,
    "page1_attempts": 2,
    "requests": 4,
    "pages_ok": [1, 2],
    "parsed": 40,
    "new_rows": 3
  }
}
```

A quiet weekend should show a fresh heartbeat with `new_rows: 0` and an older
`published_date`; preflight should stay green. A dead collector should fail the
heartbeat check even if the market is closed.
