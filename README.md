# market

Personal market-signal stack for Bursa Malaysia: news collection + scoring, an
insider-filing pipeline, FX and index signal engines, and a Flask dashboard.

## Dashboard tabs

| Tab | What it shows |
|---|---|
| `/` | collected headlines, scored with relevance / sector / sentiment |
| `/insider` | insider (director + substantial holder) signals from Bursa filings |
| `/movers` | top 5 insider-active stocks with 1-year candles and price-vs-insider metrics |
| `/top` | **one stock**: the highest-probability mover, with the arithmetic and a chart |

The dashboard runs on port 5000: `python3 app.py`.

## Jobs

| Path | Job |
|---|---|
| `collector.py` → `scorer.py` | RSS headlines → SQLite → rule-based relevance + FinBERT sentiment |
| `announcements_signal/` | Bursa announcements/insider pipeline → signals + Telegram |
| `fx_signal/` | FX price collection, signal engine, outcome tracking, Telegram |
| `indices_signal/` | same for indices |

## Company registry

`data/bursa_tickers.json` is the single source of truth for company names,
sectors and aliases; `tickers.py` derives the alias variants and validates the
file. See **[`data/README.md`](data/README.md)**.

```bash
python3 tickers.py check
python3 tickers.py suggest --days 7
```

## Keeping the server and GitHub in sync

`autosync.sh` commits server edits, pulls, and pushes every 5 minutes via
`deploy/market-autosync.timer` — both directions, never force-pushing. See
**[`WORKFLOW.md`](WORKFLOW.md)**.

## Where to start

- **[`HANDOFF.md`](HANDOFF.md)** — current state, what changed, deploy routine, known follow-ups.
- **[`WORKFLOW.md`](WORKFLOW.md)** — day-to-day server workflows, autosync, deploy cheat-sheet.
- **[`data/README.md`](data/README.md)** — maintaining the company registry.

## Tests

```bash
python3 -m unittest test_tickers.py test_top_mover.py test_insider_movers.py test_yahoo_client.py
cd announcements_signal && python3 -m unittest test_telegram_bot.py
./test_autosync.sh
```

For information only, not investment advice.
