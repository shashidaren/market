#!/usr/bin/env python3
"""
Insider trade detail parser.

Fetches disclosure pages from disclosure.bursamalaysia.com and extracts
transaction details for all INSIDER_TRADE announcements.

Usage:
    python3 parse_detail.py --stock 8052
    python3 parse_detail.py --days 7
    python3 parse_detail.py --all
    python3 parse_detail.py --report --stock 8052
"""

import argparse
import logging
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import cloudscraper
from bs4 import BeautifulSoup

from collector import DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("parse_detail")

DISCLOSURE_URL = "https://disclosure.bursamalaysia.com/FileAccess/viewHtml?e={ann_id}"
SLEEP_BETWEEN_REQUESTS = 2.0


# ─── Helpers ───────────────────────────────────────────────────────

def normalize_date(date_str: str | None) -> str | None:
    """
    Convert Bursa date formats to ISO YYYY-MM-DD.
    Handles:
        - DD/MM/YYYY   (e.g. 13/08/2026)
        - DD MMM YYYY  (e.g. 12 Aug 2026)
        - Already ISO  (e.g. 2026-08-12)
    """
    if not date_str:
        return None
    date_str = date_str.strip()

    # DD/MM/YYYY → YYYY-MM-DD
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", date_str)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    # DD MMM YYYY → YYYY-MM-DD
    try:
        dt = datetime.strptime(date_str, "%d %b %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    # Already ISO
    if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
        return date_str

    return None


# ─── DB layer ──────────────────────────────────────────────────────

def init_detail_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bursa_insider_details (
            ann_id                  INTEGER PRIMARY KEY,
            stock_code              TEXT,
            company_name            TEXT,
            published_date          TEXT,
            person_name             TEXT,
            transaction_type        TEXT,
            nature_of_interest      TEXT,
            shares_transacted       INTEGER,
            consideration           TEXT,
            price_per_share         REAL,
            direct_units_after      INTEGER,
            direct_pct_after        REAL,
            indirect_units_after    INTEGER,
            indirect_pct_after      REAL,
            date_of_change          TEXT,
            date_of_notice          TEXT,
            circumstances           TEXT,
            parsed_at               TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_bid_stock
        ON bursa_insider_details(stock_code)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_bid_date
        ON bursa_insider_details(published_date)
    """)
    conn.commit()


def get_announcements_to_parse(
    conn: sqlite3.Connection,
    stock_code: str | None = None,
    days: int | None = None,
) -> list[dict]:
    query = """
        SELECT ba.ann_id, ba.stock_code, ba.company_name,
               ba.published_date, ba.title, ba.url
        FROM bursa_announcements ba
        LEFT JOIN bursa_insider_details bid ON ba.ann_id = bid.ann_id
        WHERE ba.category = 'INSIDER_TRADE'
        AND bid.ann_id IS NULL
    """
    params = []

    if stock_code:
        query += " AND ba.stock_code = ?"
        params.append(stock_code)

    if days:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        query += " AND ba.published_date >= ?"
        params.append(cutoff)

    query += " ORDER BY ba.published_date DESC"
    cur = conn.execute(query, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def store_detail(conn: sqlite3.Connection, detail: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO bursa_insider_details (
            ann_id, stock_code, company_name, published_date,
            person_name, transaction_type, nature_of_interest,
            shares_transacted, consideration, price_per_share,
            direct_units_after, direct_pct_after,
            indirect_units_after, indirect_pct_after,
            date_of_change, date_of_notice,
            circumstances, parsed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        detail["ann_id"],
        detail["stock_code"],
        detail.get("company_name"),
        detail["published_date"],
        detail.get("person_name"),
        detail.get("transaction_type"),
        detail.get("nature_of_interest"),
        detail.get("shares_transacted"),
        detail.get("consideration"),
        detail.get("price_per_share"),
        detail.get("direct_units_after"),
        detail.get("direct_pct_after"),
        detail.get("indirect_units_after"),
        detail.get("indirect_pct_after"),
        detail.get("date_of_change"),
        detail.get("date_of_notice"),
        detail.get("circumstances"),
        now,
    ))
    conn.commit()


# ─── Parsing layer ─────────────────────────────────────────────────

def clean_int(text: str) -> int | None:
    """'100,000' → 100000"""
    if not text:
        return None
    cleaned = re.sub(r"[,\s]", "", text.strip())
    try:
        return int(cleaned)
    except ValueError:
        return None


def clean_float(text: str) -> float | None:
    """'0.004' or 'RM0.87' → float"""
    if not text:
        return None
    cleaned = re.sub(r"[,\sRM]", "", text.strip())
    try:
        return float(cleaned)
    except ValueError:
        return None


def get_label_value(soup: BeautifulSoup, label_text: str) -> str | None:
    """
    Find a <td> whose text matches label_text, return the next
    sibling <td>'s text. Works for both formContentLabel and
    formTableColumnHeader patterns.
    """
    for td in soup.find_all("td"):
        if td.get_text(strip=True).lower() == label_text.lower():
            sibling = td.find_next_sibling("td")
            if sibling:
                return sibling.get_text(strip=True)
    return None


def parse_disclosure_html(html: str, ann: dict) -> dict:
    """
    Parse disclosure.bursamalaysia.com HTML.
    Handles both S219 (Director) and S138 (Substantial Shareholder) forms.
    S138 can have MULTIPLE transaction rows - we sum them all.
    """
    soup = BeautifulSoup(html, "html.parser")

    result = {
        "ann_id":               ann["ann_id"],
        "stock_code":           ann["stock_code"],
        "company_name":         ann.get("company_name"),
        "published_date":       ann["published_date"],
        "person_name":          None,
        "transaction_type":     None,
        "nature_of_interest":   None,
        "shares_transacted":    None,
        "consideration":        None,
        "price_per_share":      None,
        "direct_units_after":   None,
        "direct_pct_after":     None,
        "indirect_units_after": None,
        "indirect_pct_after":   None,
        "date_of_change":       None,
        "date_of_notice":       None,
        "circumstances":        None,
    }

    # ── Person / entity name ─────────────────────────────
    name_val = get_label_value(soup, "Name")
    if name_val:
        result["person_name"] = re.sub(r"\s+", " ", name_val).strip()
    else:
        m = re.search(r"-\s+(.+)$", ann.get("title", ""))
        if m:
            result["person_name"] = m.group(1).strip()

    # ── ven_table: Details of changes ────────────────────
    # S138 can have multiple transaction rows - collect ALL of them
    ven_table = soup.find("table", class_="ven_table")

    total_acquired  = 0
    total_disposed  = 0
    tx_types_seen   = set()
    natures_seen    = set()
    last_date       = None

    if ven_table:
        for row in ven_table.find_all("tr", valign="top"):
            cells = row.find_all("td")
            if not cells:
                continue
            cell_texts = [c.get_text(separator=" ", strip=True) for c in cells]

            # ── Identify data row by date ─────────────────
            date_found = None
            for ct in cell_texts:
                # S219: 13/08/2026
                m = re.search(r"\d{2}/\d{2}/\d{4}", ct)
                if m:
                    date_found = m.group(0)
                    break
                # S138: 12 Aug 2026
                m = re.search(r"\d{1,2}\s+[A-Za-z]{3}\s+\d{4}", ct)
                if m:
                    date_found = m.group(0)
                    break

            if not date_found:
                # Check for consideration row (S219)
                for ct in cell_texts:
                    if "consideration" in ct.lower():
                        idx = cell_texts.index(ct)
                        if idx + 1 < len(cell_texts):
                            consid = cell_texts[idx + 1].strip()
                            if consid:
                                result["consideration"] = consid
                                result["price_per_share"] = clean_float(consid)
                        break
                continue

            # We have a data row
            last_date = date_found

            # ── Transaction type for this row ─────────────
            row_tx = None
            for ct in cell_texts:
                if re.search(r"\bacquir", ct, re.IGNORECASE):
                    row_tx = "ACQUISITION"
                    break
                elif re.search(r"\bdispos", ct, re.IGNORECASE):
                    row_tx = "DISPOSAL"
                    break
            if row_tx:
                tx_types_seen.add(row_tx)

            # ── Shares for this row ───────────────────────
            row_shares = None
            for ct in cell_texts:
                # Skip date strings and short strings
                if re.search(r"\d{1,2}[/\s][A-Za-z0-9]{2,3}[/\s]\d{4}", ct):
                    continue
                num = clean_int(ct)
                if num and num > 0:
                    row_shares = num
                    break

            if row_shares:
                if row_tx == "ACQUISITION":
                    total_acquired += row_shares
                elif row_tx == "DISPOSAL":
                    total_disposed += row_shares

            # ── Nature of interest for this row ──────────
            for ct in cell_texts:
                if re.search(r"indirect|deemed", ct, re.IGNORECASE):
                    natures_seen.add("Indirect")
                    break
                elif re.search(r"direct", ct, re.IGNORECASE):
                    natures_seen.add("Direct")
                    break

    # ── Consolidate multi-row results ─────────────────────
    result["date_of_change"] = normalize_date(last_date)

    # Net shares and transaction type
    if total_acquired > 0 and total_disposed == 0:
        result["transaction_type"]  = "ACQUISITION"
        result["shares_transacted"] = total_acquired
    elif total_disposed > 0 and total_acquired == 0:
        result["transaction_type"]  = "DISPOSAL"
        result["shares_transacted"] = total_disposed
    elif total_acquired > 0 and total_disposed > 0:
        # Mixed - report net
        net = total_acquired - total_disposed
        result["transaction_type"]  = "ACQUISITION" if net > 0 else "DISPOSAL"
        result["shares_transacted"] = abs(net)
        log.info(
            "  Mixed tx: acquired=%d disposed=%d net=%d",
            total_acquired, total_disposed, net
        )

    # Nature - if both Direct and Indirect seen, report Both
    if "Direct" in natures_seen and "Indirect" in natures_seen:
        result["nature_of_interest"] = "Both"
    elif "Direct" in natures_seen:
        result["nature_of_interest"] = "Direct"
    elif "Indirect" in natures_seen:
        result["nature_of_interest"] = "Indirect"

    # ── Consideration fallback ────────────────────────────
    if not result["consideration"]:
        consid_val = get_label_value(soup, "Consideration (if any)")
        if consid_val and consid_val.strip():
            result["consideration"] = consid_val
            result["price_per_share"] = clean_float(consid_val)

    # ── Total shares after change ─────────────────────────
    result["direct_units_after"]   = clean_int(
        get_label_value(soup, "Direct (units)"))
    result["direct_pct_after"]     = clean_float(
        get_label_value(soup, "Direct (%)"))
    result["indirect_units_after"] = clean_int(
        get_label_value(soup, "Indirect/deemed interest (units)"))
    result["indirect_pct_after"]   = clean_float(
        get_label_value(soup, "Indirect/deemed interest (%)"))

    # ── Date of notice ────────────────────────────────────
    result["date_of_notice"] = normalize_date(
        get_label_value(soup, "Date of notice")
    )

    # ── Circumstances ─────────────────────────────────────
    result["circumstances"] = get_label_value(
        soup, "Circumstances by reason of which change has occurred"
    )

    # ── Nature of interest fallback via label ─────────────
    if not result["nature_of_interest"]:
        nature_val = get_label_value(soup, "Nature of interest")
        if nature_val:
            if re.search(r"indirect|deemed", nature_val, re.IGNORECASE):
                result["nature_of_interest"] = "Indirect"
            elif re.search(r"direct", nature_val, re.IGNORECASE):
                result["nature_of_interest"] = "Direct"

    return result


# ─── Fetch layer ───────────────────────────────────────────────────

def fetch_and_parse(scraper, ann: dict) -> dict | None:
    """Use disclosure URL directly - bypass the JS shell page."""
    disclosure_url = DISCLOSURE_URL.format(ann_id=ann["ann_id"])
    try:
        resp = scraper.get(disclosure_url, timeout=20)
        if resp.status_code != 200:
            log.warning(
                "HTTP %d for ann_id=%s url=%s",
                resp.status_code, ann["ann_id"], disclosure_url
            )
            return None
        return parse_disclosure_html(resp.text, ann)
    except Exception:
        log.exception(
            "Failed to fetch ann_id=%s", ann["ann_id"]
        )
        return None


# ─── Report ────────────────────────────────────────────────────────

def print_summary(conn: sqlite3.Connection, stock_code: str | None = None) -> None:
    """
    Print a deduplicated summary of insider activity.

    Strategy:
    - Group by (published_date, person_name)
    - For each group, prefer S219 row (has price) but use S138 for
      total holdings (direct + indirect units/pct)
    - Show one clean row per person per day
    """
    # Get all details joined with announcement subcategory
    query = """
        SELECT
            bid.published_date,
            bid.person_name,
            bid.transaction_type,
            bid.nature_of_interest,
            bid.shares_transacted,
            bid.consideration,
            bid.price_per_share,
            bid.direct_units_after,
            bid.direct_pct_after,
            bid.indirect_units_after,
            bid.indirect_pct_after,
            bid.circumstances,
            ba.subcategory
        FROM bursa_insider_details bid
        JOIN bursa_announcements ba ON bid.ann_id = ba.ann_id
    """
    params = []
    if stock_code:
        query += " WHERE ba.stock_code = ?"
        params.append(stock_code)
    query += " ORDER BY bid.published_date DESC, bid.person_name"

    rows = conn.execute(query, params).fetchall()

    if not rows:
        print("\n  No data found. Run without --report first to fetch data.\n")
        return

    # ── Deduplicate: merge S219 + S138 per (date, person) ─────
    from collections import OrderedDict
    merged = OrderedDict()

    for row in rows:
        (pub_date, person, tx_type, nature, shares, consid,
         price, d_units, d_pct, i_units, i_pct,
         circumstances, subcategory) = row

        key = (pub_date, person)

        if key not in merged:
            merged[key] = {
                "published_date":       pub_date,
                "person_name":          person,
                "transaction_type":     None,
                "nature_of_interest":   None,
                "shares_transacted":    None,
                "consideration":        None,
                "price_per_share":      None,
                "direct_units_after":   None,
                "direct_pct_after":     None,
                "indirect_units_after": None,
                "indirect_pct_after":   None,
                "total_units_after":    None,
                "total_pct_after":      None,
                "circumstances":        None,
            }

        rec = merged[key]

        # S219 has price — prefer it for tx details
        if subcategory == "DIRECTOR_S219":
            if tx_type:
                rec["transaction_type"] = tx_type
            if shares:
                rec["shares_transacted"] = shares
            if consid:
                rec["consideration"] = consid
            if price:
                rec["price_per_share"] = price
            if nature:
                rec["nature_of_interest"] = nature
            if circumstances:
                rec["circumstances"] = circumstances

        # S138 has indirect holdings — prefer it for after-totals
        if subcategory == "SUBSTANTIAL_S138":
            if not rec["transaction_type"] and tx_type:
                rec["transaction_type"] = tx_type
            if not rec["shares_transacted"] and shares:
                rec["shares_transacted"] = shares
            if not rec["nature_of_interest"] and nature:
                rec["nature_of_interest"] = nature
            if not rec["circumstances"] and circumstances:
                rec["circumstances"] = circumstances
            # S138 always has better indirect numbers
            if i_units is not None:
                rec["indirect_units_after"] = i_units
            if i_pct is not None:
                rec["indirect_pct_after"] = i_pct

        # Both can have direct units
        if d_units is not None:
            rec["direct_units_after"] = d_units
        if d_pct is not None:
            rec["direct_pct_after"] = d_pct

    # Calculate totals
    for rec in merged.values():
        d = rec["direct_units_after"] or 0
        i = rec["indirect_units_after"] or 0
        rec["total_units_after"] = d + i if (d or i) else None
        dp = rec["direct_pct_after"] or 0
        ip = rec["indirect_pct_after"] or 0
        rec["total_pct_after"] = dp + ip if (dp or ip) else None

    # ── Print ─────────────────────────────────────────────
    print(f"\n{'='*120}")
    print(f"  INSIDER ACTIVITY SUMMARY" + (f" — Stock {stock_code}" if stock_code else ""))
    print(f"  (Deduplicated: S219 for price, S138 for total holdings)")
    print(f"{'='*120}")
    print(f"  {'Date':<12} {'Person':<35} {'Type':<13} {'Nature':<10} "
          f"{'Shares':>12} {'Price':>8} {'Direct':>14} {'Indirect':>14} {'Total%':>8}")
    print(f"  {'-'*12} {'-'*35} {'-'*13} {'-'*10} "
          f"{'-'*12} {'-'*8} {'-'*14} {'-'*14} {'-'*8}")

    buy_total = 0
    sell_total = 0

    for rec in merged.values():
        tx = rec["transaction_type"]
        shares = rec["shares_transacted"]
        price = rec["consideration"]
        d_after = rec["direct_units_after"]
        i_after = rec["indirect_units_after"]
        t_pct = rec["total_pct_after"]
        person = str(rec["person_name"] or "?")[:35]

        shares_str = f"{shares:,}" if shares else "?"
        price_str = price if price else "-"
        d_str = f"{d_after:,}" if d_after else "-"
        i_str = f"{i_after:,}" if i_after else "-"
        pct_str = f"{t_pct:.3f}%" if t_pct else "?"

        if tx == "ACQUISITION":
            icon = "✅ "
            buy_total += (shares or 0)
        elif tx == "DISPOSAL":
            icon = "🔴 "
            sell_total += (shares or 0)
        else:
            icon = "⚠️  "

        print(f"  {rec['published_date']:<12} {person:<35} {icon}{str(tx or 'UNK'):<10} "
              f"{str(rec['nature_of_interest'] or '?'):<10} "
              f"{shares_str:>12} {price_str:>8} {d_str:>14} {i_str:>14} {pct_str:>8}")

        if tx == "ACQUISITION" and shares:
            buy_total += 0  # already counted
        elif tx == "DISPOSAL" and shares:
            sell_total += 0

    net = buy_total - sell_total
    direction = "NET BUYING 🚀" if net > 0 else "NET SELLING ⚠️" if net < 0 else "NEUTRAL"

    print(f"{'='*120}")
    print(f"  Total ACQUIRED : {buy_total:>15,} shares")
    print(f"  Total DISPOSED : {sell_total:>15,} shares")
    print(f"  Net            : {net:>15,} shares  → {direction}")
    print(f"{'='*120}\n")

# ─── Main ──────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse Bursa insider trade detail pages"
    )
    parser.add_argument("--stock", help="Filter by stock code e.g. 8052")
    parser.add_argument("--days",  type=int, help="Only process last N days")
    parser.add_argument("--all",   action="store_true",
                        help="Process all unprocessed records")
    parser.add_argument("--report", action="store_true",
                        help="Print summary report only, no fetching")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_detail_table(conn)

    if args.report:
        print_summary(conn, args.stock)
        conn.close()
        return

    days = args.days if args.days else (None if args.all else 7)
    announcements = get_announcements_to_parse(conn, args.stock, days)

    log.info(
        "Found %d unprocessed insider announcements to parse",
        len(announcements)
    )

    if not announcements:
        log.info("Nothing to fetch. Showing existing data:")
        print_summary(conn, args.stock)
        conn.close()
        return

    scraper = cloudscraper.create_scraper()
    success = 0
    failed = 0

    for i, ann in enumerate(announcements, 1):
        log.info(
            "[%d/%d] ann_id=%s %s — %s",
            i, len(announcements),
            ann["ann_id"], ann["published_date"],
            ann["title"][:60]
        )

        detail = fetch_and_parse(scraper, ann)
        if detail:
            store_detail(conn, detail)
            tx   = detail.get("transaction_type", "UNKNOWN")
            shrs = detail.get("shares_transacted")
            cons = detail.get("consideration")
            log.info(
                "  → %-12s | shares=%-12s | consideration=%s",
                tx,
                f"{shrs:,}" if shrs else "?",
                cons or "?",
            )
            success += 1
        else:
            failed += 1

        if i < len(announcements):
            time.sleep(SLEEP_BETWEEN_REQUESTS)

    log.info("Done. success=%d  failed=%d", success, failed)
    print_summary(conn, args.stock)
    conn.close()


if __name__ == "__main__":
    main()
