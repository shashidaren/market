# `data/` — the Bursa company registry

`bursa_tickers.json` is the single source of truth for *"which company is this
headline about, and which sector is it in"*. `tickers.py` loads it and derives
the alias variants; `scorer.py`, the dashboard and `top_mover.py` all read the
same registry.

You do **not** need to write Python to maintain it. Edit the JSON (or use
`tickers.py add`), run `python3 tickers.py check`, done.

## Files

| File | What it holds |
|---|---|
| `bursa_tickers.json` | one record per company (listed + the foreign/macro names we track) |
| `macro_keywords.json` | theme keywords ("OPR", "Fed", "rate cut") → sector, used when no company matches |

## One company, one record

```json
{
  "code": "8869",
  "name": "Press Metal Aluminium",
  "legal_name": "Press Metal Aluminium Holdings Bhd",
  "sector": "Materials",
  "aliases": ["Press Metal", "PMetal"]
}
```

| Field | Required | Meaning |
|---|---|---|
| `code` | no | Bursa stock code, four digits. `null`/missing = not a Bursa-listed name (e.g. `EPF`, `FBM KLCI`, `SK Hynix`). |
| `name` | yes | Display/canonical name. This is what the dashboard and `scored_headlines.tickers` show. |
| `sector` | yes | Must be one of `KNOWN_SECTORS` in `tickers.py` — `check` warns on anything else. |
| `legal_name` | no | Adds aliases automatically (see below). |
| `aliases` | no | Extra phrasings actually seen in headlines. |

### What is generated for you

From the example above the matcher accepts **Press Metal Aluminium**,
**Press Metal Aluminium Holdings Bhd**, **Press Metal Aluminium Holdings**,
**Press Metal**, **PMetal** and **8869** — without you typing the variants.
The old file needed one line per phrasing:

```python
"Maybank":             ("Maybank", "Banking"),
"Malayan Banking":     ("Maybank", "Banking"),
"Malayan Banking Bhd": ("Maybank", "Banking"),
"1155":                ("Maybank", "Banking"),
```

Now that is:

```json
{"code": "1155", "name": "Maybank", "legal_name": "Malayan Banking Bhd", "sector": "Banking"}
```

### Deliberately *not* generated

* dropping `Group` / `Holdings` — "Hong Leong" is three different listed
  companies, so a derived alias like that would be worse than useless;
* the first word of a name — "Public" is not Public Bank.

If an alias would belong to two companies, it is **ignored** and reported by
`check`; the more specific names keep working.

## Everyday commands

```bash
python3 tickers.py check                    # validate + statistics
python3 tickers.py check --verbose          # also list short/ambiguous aliases
python3 tickers.py match "Maybank, CIMB up on OPR hold"
python3 tickers.py list --sector Plantation
python3 tickers.py list --search press

# add a new listing (this is the whole job)
python3 tickers.py add --code 8869 --name "Press Metal" \
    --legal-name "Press Metal Aluminium Holdings Bhd" \
    --sector Materials --alias PMetal

# add/remove a nickname later
python3 tickers.py alias --code 8869 --add "PMetal"
python3 tickers.py alias --code 8869 --remove "PMetal"

# what are we missing? ranks names in headlines the scorer called "noise"
python3 tickers.py suggest --days 7
```

`add` is idempotent: run it again with the same `--code` to update the sector
or legal name, and aliases are merged, never duplicated.

## Matching rules

* case-insensitive, with word boundaries — `Fed` does not match inside
  `Federal`, `TM` does not match inside `ATM`;
* stock codes match as whole numbers (`1155`, and `1155` inside `1155.KL`);
* companies are checked before macro keywords; a matched company wins;
* a headline that matches several companies lists all of them (the scorer's
  ticker clustering relies on this).

## When the world changes

| Event | What to do |
|---|---|
| New listing / IPO | `tickers.py add ...` |
| Delisting / merger | remove the record (or keep it with a `"note"`-style comment? JSON has no comments — delete it; git has the history) |
| Company renames | change `name`, move the old name to `aliases` so old headlines still match |
| New sector | add it to `KNOWN_SECTORS` in `tickers.py` first, then use it |
| Headlines keep falling through to noise | `tickers.py suggest --days 14` |

After editing, the scorer picks the change up on its next run (it reads the
file at import time). **Restart `market-dashboard`** so the top-mover/news
routes see the new registry.

## Why JSON instead of a spreadsheet or a database

* git diffs are readable and reviewable — one changed line per changed field;
* no schema migration, no server dependency, no accidental `news.db` coupling;
* `tickers.py add` still gives spreadsheet-like editing without merge pain
  (it rewrites the file atomically and keeps the sort order).

If you ever want to edit it in Excel, `tickers.py list --json > tickers.json`
gives you a flattened view; the canonical file stays the one in git.
