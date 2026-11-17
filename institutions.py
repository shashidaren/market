#!/usr/bin/env python3
"""
Institutional flows — what the big Malaysian funds are BUYING or SELLING.

Powers the dashboard's /institutions tab (see app.py). It answers one
question, with the most recent filings first:

    "EPF / KWAP / Amanah Saham Bumiputera just reported a change —
     which stock, which direction, how many shares?"

Where the data comes from
------------------------
The SAME tables the /insider tab reads. When a fund crosses or changes a
5%+ stake it must file a "Changes in Sub. S-hldr's Int (Section 138)"
notice; the announcements pipeline (announcements_signal/) stores the
filing in `bursa_announcements` and parse_detail.py fills
`bursa_insider_details` — including `person_name`, which for these
filings is the FILER (the fund), not a director.

So there is no new cron job and no Yahoo request: this module only reads
`SUBSTANTIAL_S138` rows whose filer name matches the fund catalog below.

What the tab shows
------------------
- recent   : every fund filing in the window, newest trade date first
- summary  : bought / sold / net per fund (plus its top buy and sell)
- stocks   : per stock, the net flow of all tracked funds combined
- unmatched: S138 filers that are not in the catalog, so the catalog can
             be extended when a new fund shows up

Fund matching is deliberately conservative
------------------------------------------
Short codes ("ASB", "KWSP") only match when they are the whole filer name
or a known phrase — a company called "ASB Resources Sdn Bhd" must not be
counted as Amanah Saham Bumiputera. Long phrases ("AMANAH SAHAM
BUMIPUTERA", "KUMPULAN WANG PERSARAAN") match anywhere in the name.

Dedupe
------
A fund can re-file the same trade (correction, or a notice filed through
both the manager and the trustee). Filings are keyed by
(fund, stock, trade date, side, shares) and the duplicate is dropped so
one deal is not counted twice.

Run it directly
---------------
    python3 institutions.py                       # last 90 days, all tracked funds
    python3 institutions.py --days 30 --fund EPF,KWAP
    python3 institutions.py --side buy --limit 40
    python3 institutions.py --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("institutions")

_REPO_ROOT = Path(__file__).resolve().parent

# Knobs can live in the central /opt/market/.env like everything else
# (existing process env always wins — see env_loader.py).
try:  # pragma: no cover - env_loader always exists in this repo
    from env_loader import load_env

    load_env()
except Exception:  # pragma: no cover
    pass

DB_PATH = _REPO_ROOT / "news.db"

# Institutional flows are disclosed under Section 138 (substantial
# shareholder). Director filings (S219) are individuals, not funds.
INSIDER_SUBCAT = "SUBSTANTIAL_S138"

DEFAULT_DAYS = 90        # funds file less often than directors
MIN_DAYS = 7
MAX_DAYS = 730

DEFAULT_LIMIT = 200      # recent filings returned to the tab
MAX_LIMIT = 500
MAX_STOCKS = 100         # rows in the by-stock board

# A Bursa share price above this is almost certainly a filing that quoted
# the TOTAL consideration ("RM1,250,000") instead of a per-share price.
MAX_PLAUSIBLE_PRICE = 150.0

DISCLAIMER = (
    "Computed from Bursa Malaysia substantial-shareholder (Section 138) "
    "filings collected by this server. Filings lag the trade date, values "
    "are only shown when the filing states a per-share price, and a busy "
    "announcement day can leave gaps. Informational only — not investment "
    "advice."
)


# ─── Fund catalog ──────────────────────────────────────────────────
#
# One record per fund. `tracked` = selected by default on the tab (the
# three the user asked for); the rest are available in the fund chips.
# Patterns are matched case-insensitively against the filer name.
# Short codes are ANCHORED (^...$) on purpose — see the docstring.

@dataclass(frozen=True)
class Fund:
    key: str
    label: str
    full_name: str
    tracked: bool
    patterns: tuple[str, ...]


FUNDS: tuple[Fund, ...] = (
    Fund(
        key="EPF",
        label="EPF",
        full_name="Employees Provident Fund Board (KWSP)",
        tracked=True,
        patterns=(
            r"\bemployees?\s+provident\s+fund\b",
            r"\bepf\b",
            r"\bkwsp\b",
            r"\bkumpulan\s+wang\s+simpanan\s+pekerja\b",
        ),
    ),
    Fund(
        key="KWAP",
        label="KWAP",
        full_name="Kumpulan Wang Persaraan (Diperbadankan)",
        tracked=True,
        patterns=(
            r"\bkwap\b",
            r"\bkumpulan\s+wang\s+persaraan\b",
            r"\bretirement\s+fund\b",   # "Retirement Fund (Incorporated)"
        ),
    ),
    Fund(
        key="ASB",
        label="ASB / ASNB",
        full_name="Amanah Saham Bumiputera (filed by Amanah Saham Nasional Bhd)",
        tracked=True,
        # ASB is a unit-trust fund: the S138 filer is normally the manager,
        # Amanah Saham Nasional Bhd, which files for ASB / ASM / ASN units.
        patterns=(
            r"\bamanah\s+saham\s+bumiputera\b",
            r"\bamanah\s+saham\s+nasional\b",
            r"\basnb\b",
            r"^asb(?:\s*\d+)?$",
        ),
    ),
    Fund(
        key="PNB",
        label="PNB",
        full_name="Permodalan Nasional Berhad",
        tracked=False,
        patterns=(
            r"\bpermodalan\s+nasional\b",
            r"^pnb\b",
        ),
    ),
    Fund(
        key="KHAZANAH",
        label="Khazanah",
        full_name="Khazanah Nasional Berhad",
        tracked=False,
        patterns=(r"\bkhazanah\b",),
    ),
    Fund(
        key="TABUNG_HAJI",
        label="Tabung Haji",
        full_name="Lembaga Tabung Haji",
        tracked=False,
        patterns=(r"\btabung\s+haji\b", r"\blembaga\s+urusan\s+dan\s+tabung\s+haji\b"),
    ),
    Fund(
        key="LTAT",
        label="LTAT",
        full_name="Lembaga Tabung Angkatan Tentera",
        tracked=False,
        patterns=(r"\blembaga\s+tabung\s+angkatan\s+tentera\b", r"\bltat\b"),
    ),
    Fund(
        key="URUSHARTA",
        label="Urusharta Jamaah",
        full_name="Urusharta Jamaah Sdn Bhd",
        tracked=False,
        patterns=(r"\burusharta\s+jamaah\b",),
    ),
    Fund(
        key="VALUECAP",
        label="ValueCap",
        full_name="ValueCap Sdn Bhd",
        tracked=False,
        patterns=(r"\bvaluecap\b",),
    ),
    Fund(
        key="MOF",
        label="MOF Inc",
        full_name="Minister of Finance (Incorporated)",
        tracked=False,
        patterns=(
            r"^minister\s+of\s+finance",
            r"^ministry\s+of\s+finance",
            r"^mof\b",
        ),
    ),
)

_FUND_BY_KEY = {f.key: f for f in FUNDS}
_FUND_PATTERNS = tuple(
    (fund, tuple(re.compile(p, re.IGNORECASE) for p in fund.patterns))
    for fund in FUNDS
)


# ─── Small helpers ─────────────────────────────────────────────────

def _norm_name(name: str | None) -> str:
    """Uppercase, collapse whitespace, trim punctuation at the edges."""
    text = re.sub(r"\s+", " ", (name or "")).strip(" .,;:-")
    return text.upper()


def classify_fund(name: str | None) -> Fund | None:
    """Return the Fund a substantial-holder name belongs to, or None."""
    text = _norm_name(name)
    if not text:
        return None
    for fund, patterns in _FUND_PATTERNS:
        if any(p.search(text) for p in patterns):
            return fund
    return None


def fund_catalog() -> list[dict]:
    """Catalog for the UI (no regexes)."""
    return [
        {
            "key": f.key,
            "label": f.label,
            "full_name": f.full_name,
            "tracked": f.tracked,
        }
        for f in FUNDS
    ]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _days_between(base_date, later_date) -> int | None:
    """Calendar days between two ISO dates (filing lag)."""
    a, b = _parse_day(base_date), _parse_day(later_date)
    if not a or not b:
        return None
    return (b - a).days


def per_share_price(consideration: str | None,
                    price_per_share: float | None) -> float | None:
    """
    Best-effort per-share price from a filing.

    parse_detail.py fills `price_per_share` with clean_float(consideration),
    which on an S138 form can be the TOTAL value. Accept a number only when
    it is a plausible share price; otherwise return None and the tab shows
    the raw consideration text instead of inventing a value.
    """
    if price_per_share and 0 < float(price_per_share) <= MAX_PLAUSIBLE_PRICE:
        return float(price_per_share)
    if not consideration:
        return None

    # "RM0.845 per share" -> 0.845 ; "RM0.85 - RM0.90" -> 0.875
    numbers = []
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", str(consideration)):
        try:
            numbers.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    numbers = [n for n in numbers if 0 < n <= MAX_PLAUSIBLE_PRICE]
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2 and re.search(r"-|–|\bto\b", str(consideration), re.I):
        return round((numbers[0] + numbers[1]) / 2, 4)
    return None


# ─── DB layer ──────────────────────────────────────────────────────

def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def fetch_rows(conn: sqlite3.Connection, days: int) -> list[dict]:
    """
    Every Section 138 filing published in the last `days` days.
    Tolerates older DBs missing the optional columns.
    """
    cols = _table_columns(conn, "bursa_insider_details")
    if not cols:
        raise sqlite3.OperationalError("no such table: bursa_insider_details")
    if not _table_columns(conn, "bursa_announcements"):
        raise sqlite3.OperationalError("no such table: bursa_announcements")

    def col(name: str) -> str:
        return f"bid.{name}" if name in cols else f"NULL AS {name}"

    optional = ", ".join(col(c) for c in (
        "date_of_change", "consideration", "price_per_share",
        "direct_pct_after", "indirect_pct_after"))

    query = f"""
        SELECT bid.ann_id, bid.stock_code, bid.company_name,
               bid.published_date, bid.person_name, bid.transaction_type,
               bid.shares_transacted, {optional}, ba.url
        FROM bursa_insider_details bid
        JOIN bursa_announcements ba ON bid.ann_id = ba.ann_id
        WHERE ba.subcategory = ?
          AND bid.published_date >= date('now', ?)
          AND bid.stock_code IS NOT NULL
        ORDER BY bid.published_date DESC, bid.ann_id DESC
    """
    cur = conn.execute(query, (INSIDER_SUBCAT, f"-{int(days)} days"))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


# ─── Entry building ────────────────────────────────────────────────

def entry_from_row(row: dict, fund: Fund) -> dict | None:
    """One filing -> one feed entry (None when it carries no usable flow)."""
    side = {"ACQUISITION": "buy", "DISPOSAL": "sell"}.get(
        (row.get("transaction_type") or "").upper())
    if not side:
        return None
    try:
        shares = int(row.get("shares_transacted") or 0)
    except (TypeError, ValueError):
        shares = 0
    if shares <= 0:
        return None

    price = per_share_price(row.get("consideration"), row.get("price_per_share"))
    trade_date = (row.get("date_of_change") or "")[:10] or None
    published = (row.get("published_date") or "")[:10] or None
    sort_date = max(trade_date or "", published or "") or None

    direct = row.get("direct_pct_after")
    indirect = row.get("indirect_pct_after")
    pct_after = None
    if direct is not None or indirect is not None:
        pct_after = round((direct or 0) + (indirect or 0), 3)

    return {
        "ann_id": row.get("ann_id"),
        "fund": fund.key,
        "fund_label": fund.label,
        "person_name": row.get("person_name"),
        "stock_code": str(row.get("stock_code")).strip().upper(),
        "company_name": row.get("company_name"),
        "symbol": f"{str(row.get('stock_code')).strip().upper().zfill(4)}.KL",
        "side": side,
        "shares": shares,
        "trade_date": trade_date,
        "published_date": published,
        "sort_date": sort_date,
        "lag_days": _days_between(trade_date, published),
        "price": price,
        "value_rm": round(shares * price, 2) if price else None,
        "consideration": row.get("consideration"),
        "pct_after": pct_after,
        "direct_pct_after": direct,
        "indirect_pct_after": indirect,
        "url": row.get("url"),
    }


def collect_entries(rows: list[dict]) -> tuple[list[dict], Counter, int]:
    """
    Classify + dedupe all rows.

    Returns (entries, unmatched_name_counter, duplicates_dropped).
    Entries are newest first. The duplicate key keeps the fund, so EPF and
    KWAP buying the same size on the same day both survive.
    """
    entries: list[dict] = []
    unmatched: Counter = Counter()
    seen: set[tuple] = set()
    duplicates = 0

    for row in rows:
        name = _norm_name(row.get("person_name"))
        fund = classify_fund(name)
        if fund is None:
            if name:
                unmatched[name] += 1
            continue
        entry = entry_from_row(row, fund)
        if entry is None:
            continue
        key = (fund.key, entry["stock_code"], entry["sort_date"],
               entry["side"], entry["shares"])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        entries.append(entry)

    entries.sort(
        key=lambda e: (e["sort_date"] or "", e["published_date"] or "", e["ann_id"] or 0),
        reverse=True,
    )
    return entries, unmatched, duplicates


# ─── Aggregation ───────────────────────────────────────────────────

def summarize_funds(entries: list[dict], keys: list[str]) -> list[dict]:
    """Per-fund bought / sold / net, plus the biggest buy and sell."""
    agg: dict[str, dict] = {}
    for key in keys:
        fund = _FUND_BY_KEY[key]
        agg[key] = {
            "key": fund.key,
            "label": fund.label,
            "full_name": fund.full_name,
            "bought": 0,
            "sold": 0,
            "n_filings": 0,
            "last_date": None,
            "bought_value_rm": 0.0,
            "sold_value_rm": 0.0,
            "priced_filings": 0,
            "stocks": {},
            "latest": [],
        }

    for e in entries:
        a = agg.get(e["fund"])
        if a is None:
            continue
        a["n_filings"] += 1
        if e["value_rm"] is not None:
            a["priced_filings"] += 1
        if len(a["latest"]) < 3:
            a["latest"].append({
                "side": e["side"],
                "stock_code": e["stock_code"],
                "company_name": e["company_name"],
                "shares": e["shares"],
                "sort_date": e["sort_date"],
                "url": e["url"],
            })
        stock = a["stocks"].setdefault(
            e["stock_code"], {"stock_code": e["stock_code"],
                              "company_name": e["company_name"],
                              "bought": 0, "sold": 0})
        if e["side"] == "buy":
            a["bought"] += e["shares"]
            if e["value_rm"] is not None:
                a["bought_value_rm"] += e["value_rm"]
            stock["bought"] += e["shares"]
        else:
            a["sold"] += e["shares"]
            if e["value_rm"] is not None:
                a["sold_value_rm"] += e["value_rm"]
            stock["sold"] += e["shares"]
        if e["sort_date"] and (a["last_date"] is None or e["sort_date"] > a["last_date"]):
            a["last_date"] = e["sort_date"]

    out = []
    for key in keys:
        a = agg[key]
        net = a["bought"] - a["sold"]
        stocks = []
        for s in a["stocks"].values():
            s["net"] = s["bought"] - s["sold"]
            stocks.append(s)
        buys = [s for s in stocks if s["net"] > 0]
        sells = [s for s in stocks if s["net"] < 0]
        top_buy = max(buys, key=lambda s: s["net"], default=None)
        top_sell = min(sells, key=lambda s: s["net"], default=None)
        priced = a["priced_filings"] > 0
        out.append({
            "key": key,
            "label": a["label"],
            "full_name": a["full_name"],
            "bought": a["bought"],
            "sold": a["sold"],
            "net_shares": net,
            "direction": "buy" if net > 0 else "sell" if net < 0 else "flat",
            "n_filings": a["n_filings"],
            "n_stocks": len(stocks),
            "last_date": a["last_date"],
            "top_buy": _flow_brief(top_buy),
            "top_sell": _flow_brief(top_sell),
            "bought_value_rm": round(a["bought_value_rm"], 2) if priced else None,
            "sold_value_rm": round(a["sold_value_rm"], 2) if priced else None,
            "net_value_rm": (round(a["bought_value_rm"] - a["sold_value_rm"], 2)
                             if priced else None),
            "priced_filings": a["priced_filings"],
            "latest": a["latest"],
        })
    return out


def _flow_brief(stock: dict | None) -> dict | None:
    if not stock:
        return None
    return {
        "stock_code": stock["stock_code"],
        "company_name": stock["company_name"],
        "shares": abs(stock["net"]),
    }


def summarize_stocks(entries: list[dict], limit: int = MAX_STOCKS) -> list[dict]:
    """Per stock: combined bought / sold / net across the selected funds."""
    by_stock: dict[str, dict] = {}
    for e in entries:
        s = by_stock.setdefault(e["stock_code"], {
            "stock_code": e["stock_code"],
            "company_name": e["company_name"],
            "symbol": e["symbol"],
            "bought": 0,
            "sold": 0,
            "n_filings": 0,
            "last_date": None,
            "last_pct_after": None,
            "funds": {},
        })
        s["n_filings"] += 1
        fund = s["funds"].setdefault(
            e["fund"], {"key": e["fund"], "label": e["fund_label"],
                        "bought": 0, "sold": 0})
        if e["side"] == "buy":
            s["bought"] += e["shares"]
            fund["bought"] += e["shares"]
        else:
            s["sold"] += e["shares"]
            fund["sold"] += e["shares"]
        if e["sort_date"] and (s["last_date"] is None or e["sort_date"] > s["last_date"]):
            s["last_date"] = e["sort_date"]
            if e["pct_after"] is not None:
                s["last_pct_after"] = e["pct_after"]

    out = []
    for s in by_stock.values():
        s["net_shares"] = s["bought"] - s["sold"]
        s["direction"] = "buy" if s["net_shares"] > 0 else "sell" if s["net_shares"] < 0 else "flat"
        for f in s["funds"].values():
            f["net"] = f["bought"] - f["sold"]
        s["funds"] = sorted(s["funds"].values(), key=lambda f: -abs(f["net"]))
        out.append(s)

    # Biggest absolute flow first; most recent breaks ties.
    out.sort(key=lambda s: (abs(s["net_shares"]), s["last_date"] or ""), reverse=True)
    return out[:limit]


# ─── Params / payload ──────────────────────────────────────────────

def parse_params(args: dict | None = None) -> dict:
    """Clamp query-string / CLI values. `fund` accepts keys or 'all'."""
    args = args or {}

    def as_int(name: str, default: int, lo: int, hi: int) -> int:
        try:
            value = int(args.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(lo, min(hi, value))

    known = [f.key for f in FUNDS]
    raw = args.get("fund", args.get("funds", "")) or ""
    requested = [part.strip().upper() for part in str(raw).split(",") if part.strip()]
    if any(part == "ALL" for part in requested):
        funds = list(known)
    else:
        funds = [key for key in requested if key in _FUND_BY_KEY]
    if not funds:
        funds = [f.key for f in FUNDS if f.tracked]

    side = str(args.get("side", "all")).strip().lower()
    if side not in ("buy", "sell"):
        side = "all"

    default_days = max(MIN_DAYS, min(MAX_DAYS, _env_int("INSTITUTIONS_DAYS", DEFAULT_DAYS)))
    return {
        "days": as_int("days", default_days, MIN_DAYS, MAX_DAYS),
        "limit": as_int("limit", DEFAULT_LIMIT, 1, MAX_LIMIT),
        "funds": funds,
        "side": side,
    }


def build_payload(conn: sqlite3.Connection, config: dict | None = None) -> dict:
    """Everything the tab needs in one JSON document."""
    config = config or parse_params()
    today = date.today()
    payload = {
        "params": {
            "days": config["days"],
            "limit": config["limit"],
            "funds": list(config["funds"]),
            "side": config["side"],
        },
        "window": {
            "days": config["days"],
            "from": (today - timedelta(days=config["days"])).isoformat(),
            "to": today.isoformat(),
        },
        "funds": fund_catalog(),
        "summary": [],
        "recent": [],
        "recent_total": 0,
        "stocks": [],
        "counts": {},
        "unmatched": [],
        "last_activity": None,
        "error": None,
        "disclaimer": DISCLAIMER,
    }

    try:
        rows = fetch_rows(conn, config["days"])
    except sqlite3.OperationalError as exc:
        payload["error"] = (
            f"insider tables not available ({exc}) — run the "
            f"announcements_signal pipeline first"
        )
        return payload

    entries, unmatched, duplicates = collect_entries(rows)

    selected = [key for key in (f.key for f in FUNDS) if key in config["funds"]]
    scoped = [e for e in entries if e["fund"] in selected]

    feed = scoped if config["side"] == "all" else [
        e for e in scoped if e["side"] == config["side"]
    ]

    payload["summary"] = summarize_funds(scoped, selected)
    payload["recent_total"] = len(feed)
    payload["recent"] = feed[: config["limit"]]
    payload["stocks"] = summarize_stocks(scoped)
    payload["counts"] = {
        "filings": len(scoped),
        "feed_filings": len(feed),
        "funds_with_flow": sum(1 for f in payload["summary"] if f["n_filings"]),
        "stocks": len({e["stock_code"] for e in scoped}),
        "stocks_bought": len({e["stock_code"] for e in scoped if e["side"] == "buy"}),
        "stocks_sold": len({e["stock_code"] for e in scoped if e["side"] == "sell"}),
        "bought": sum(e["shares"] for e in scoped if e["side"] == "buy"),
        "sold": sum(e["shares"] for e in scoped if e["side"] == "sell"),
        "priced_filings": sum(1 for e in scoped if e["value_rm"] is not None),
        "duplicates_dropped": duplicates,
        "unmatched_filings": sum(unmatched.values()),
        "unmatched_names": len(unmatched),
    }
    payload["counts"]["net"] = payload["counts"]["bought"] - payload["counts"]["sold"]
    payload["last_activity"] = scoped[0]["sort_date"] if scoped else None

    # S138 filers outside the catalog: the list that says "add this fund".
    payload["unmatched"] = [
        {"name": name, "count": count} for name, count in unmatched.most_common(12)
    ]
    return payload


# ─── CLI ───────────────────────────────────────────────────────────

def _fmt_shares(n: int | None) -> str:
    if n is None:
        return "-"
    a = abs(n)
    if a >= 1e9:
        return f"{n / 1e9:.2f}B"
    if a >= 1e6:
        return f"{n / 1e6:.1f}M"
    if a >= 1e3:
        return f"{n / 1e3:.0f}K"
    return f"{n:,}"


def _print_report(payload: dict) -> None:
    if payload["error"]:
        print(f"[FAIL] {payload['error']}")
        return
    c = payload["counts"]
    print(f"window   : {payload['window']['from']} → {payload['window']['to']} "
          f"({payload['window']['days']}d)")
    print(f"funds    : {', '.join(payload['params']['funds'])}")
    print(f"filings  : {c['filings']}  bought {_fmt_shares(c['bought'])}  "
          f"sold {_fmt_shares(c['sold'])}  net {_fmt_shares(c['net'])}")
    print(f"stocks   : {c['stocks']} ({c['stocks_bought']} bought, "
          f"{c['stocks_sold']} sold)")
    print(f"last     : {payload['last_activity'] or '-'}")
    if c["duplicates_dropped"]:
        print(f"dedupe   : {c['duplicates_dropped']} duplicate filing(s) dropped")

    print("\nper fund")
    for f in payload["summary"]:
        if not f["n_filings"]:
            print(f"  {f['label']:<16} no filings in the window")
            continue
        top_buy = f["top_buy"]
        top_sell = f["top_sell"]
        print(f"  {f['label']:<16} net {_fmt_shares(f['net_shares']):>9}  "
              f"({_fmt_shares(f['bought'])} bought / {_fmt_shares(f['sold'])} sold)"
              f"  {f['n_filings']} filings, {f['n_stocks']} stocks, last {f['last_date']}")
        if top_buy:
            print(f"      top buy  : {top_buy['stock_code']} {top_buy['company_name']}"
                  f" +{_fmt_shares(top_buy['shares'])}")
        if top_sell:
            print(f"      top sell : {top_sell['stock_code']} {top_sell['company_name']}"
                  f" -{_fmt_shares(top_sell['shares'])}")

    print("\nlatest filings")
    if not payload["recent"]:
        print("  none")
    for e in payload["recent"][:25]:
        price = f" @ RM{e['price']:.3f}" if e["price"] else ""
        print(f"  {e['sort_date']}  {e['fund_label']:<14} "
              f"{'BUY ' if e['side'] == 'buy' else 'SELL'} "
              f"{_fmt_shares(e['shares']):>9}{price:<12} "
              f"{e['stock_code']} {e['company_name'] or ''}")
    if payload["recent_total"] > 25:
        print(f"  … {payload['recent_total'] - 25} more (use --limit)")

    if payload["unmatched"]:
        print("\nS138 filers not in the fund catalog (top 12)")
        for item in payload["unmatched"]:
            print(f"  {item['count']:>3}× {item['name'][:70]}")
    print(f"\n{payload['disclaimer']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Institutional flows from Bursa S138 filings "
                    "(EPF / KWAP / ASB and other state-linked funds).")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"window in days ({MIN_DAYS}-{MAX_DAYS}, default {DEFAULT_DAYS})")
    p.add_argument("--fund", default="",
                   help="comma-separated keys, e.g. EPF,KWAP,ASB (default: tracked)")
    p.add_argument("--side", choices=("all", "buy", "sell"), default="all")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help=f"recent filings to show (max {MAX_LIMIT})")
    p.add_argument("--db", help="path to news.db (default: repo root)")
    p.add_argument("--json", action="store_true", help="print the payload as JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db) if args.db else DB_PATH
    if not db_path.exists():
        print(f"[FAIL] no database at {db_path} — run the pipeline first")
        return 1

    config = parse_params({"days": args.days, "fund": args.fund,
                           "side": args.side, "limit": args.limit})
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        payload = build_payload(conn, config)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 0 if not payload["error"] else 1
    _print_report(payload)
    return 0 if not payload["error"] else 1


if __name__ == "__main__":
    sys.exit(main())
