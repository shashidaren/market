#!/usr/bin/env python3
"""
Bursa Malaysia announcements collector.

Fetches company announcements from the public API at:
    https://www.bursamalaysia.com/api/v1/announcements/search

The API is protected by Cloudflare's JS challenge, so we use
cloudscraper to bypass it. Runs every 10 min via cron.

    */10 * * * * cd /opt/market/announcements_signal && \
        /usr/bin/python3 collector.py >> /var/log/webscrap-bursa.log 2>&1

Layer C: fetch + parse + categorize + store in SQLite.

Uses INSERT OR IGNORE on ann_id (Bursa's unique ID) so re-runs are
idempotent - same announcement fetched twice won't create duplicates.
"""

import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import cloudscraper

from announcements_config import categorize

API_URL = "https://www.bursamalaysia.com/api/v1/announcements/search"
BASE_URL = "https://www.bursamalaysia.com"

# Shared DB with news collector - simpler for combined dashboard.
DB_PATH = Path(__file__).parent.parent / "news.db"

# Fetch this many pages per run. Each page = 20 announcements.
# Bursa gets ~200-500 announcements per day so 3 pages every 10 min
# is more than enough to keep up. First run should backfill more.
PAGES_TO_FETCH = 3
PER_PAGE = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bursa")


# ─── DB layer ──────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bursa_announcements (
            ann_id          INTEGER PRIMARY KEY,
            stock_code      TEXT,
            company_name    TEXT,
            title           TEXT NOT NULL,
            category        TEXT,
            subcategory     TEXT,
            priority        INTEGER,
            published_date  TEXT,
            url             TEXT,
            collected_at    TEXT NOT NULL
        )
        """
    )
    # Indexes for common dashboard queries
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ba_priority ON bursa_announcements(priority)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ba_stock ON bursa_announcements(stock_code)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ba_date ON bursa_announcements(published_date)"
    )
    conn.commit()


# ─── HTTP layer ────────────────────────────────────────────────────

def fetch_page(scraper, page: int = 1, per_page: int = 20) -> dict:
    """Fetch one page of announcements from Bursa API."""
    params = {
        "ann_type": "company",
        "per_page": per_page,
        "page": page,
    }
    response = scraper.get(API_URL, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


# ─── Parsing layer ─────────────────────────────────────────────────

def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_date(date_html: str) -> str | None:
    plain = strip_html(date_html)
    match = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", plain)
    if not match:
        return None
    day, month_abbr, year = match.groups()
    try:
        dt = datetime.strptime(f"{day} {month_abbr} {year}", "%d %b %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_company(company_html: str) -> tuple[str | None, str | None]:
    stock_code_match = re.search(r"stock_code=([A-Za-z0-9]+)", company_html)
    stock_code = stock_code_match.group(1) if stock_code_match else None
    name = strip_html(company_html)
    return stock_code, name or None


def parse_announcement(title_html: str) -> tuple[str | None, str, str | None]:
    ann_id_match = re.search(r"ann_id=(\d+)", title_html)
    ann_id = ann_id_match.group(1) if ann_id_match else None

    url = None
    href_match = re.search(r"href='([^']+)'", title_html)
    if href_match:
        url = BASE_URL + href_match.group(1)

    title = strip_html(title_html)
    return ann_id, title, url


def parse_record(row: list) -> dict | None:
    """Parse one Bursa API row into a categorized dict."""
    if not row or len(row) < 4:
        return None

    _row_num, date_html, company_html, title_html = row[0], row[1], row[2], row[3]

    date = parse_date(date_html)
    stock_code, company_name = parse_company(company_html)
    ann_id, title, url = parse_announcement(title_html)

    if not ann_id:
        log.warning("Row missing ann_id, skipping: %s", title[:80])
        return None

    category, subcategory, priority = categorize(title)

    return {
        "ann_id": int(ann_id),
        "stock_code": stock_code,
        "company_name": company_name,
        "title": title,
        "category": category,
        "subcategory": subcategory,
        "priority": priority,
        "published_date": date,
        "url": url,
    }


# ─── Storage layer ─────────────────────────────────────────────────

def store_records(conn: sqlite3.Connection, records: list[dict]) -> int:
    """Insert records, returns count of NEW rows added (dedup by ann_id)."""
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for r in records:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO bursa_announcements
                (ann_id, stock_code, company_name, title,
                 category, subcategory, priority,
                 published_date, url, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["ann_id"], r["stock_code"], r["company_name"], r["title"],
                r["category"], r["subcategory"], r["priority"],
                r["published_date"], r["url"], now,
            ),
        )
        if cur.rowcount:
            inserted += 1
    conn.commit()
    return inserted


# ─── Main ──────────────────────────────────────────────────────────

def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    scraper = cloudscraper.create_scraper()
    total_seen = 0
    total_new = 0

    for page in range(1, PAGES_TO_FETCH + 1):
        try:
            data = fetch_page(scraper, page=page, per_page=PER_PAGE)
        except Exception:
            log.exception("Failed to fetch page %d", page)
            continue

        raw_rows = data.get("data", [])
        parsed = [r for r in (parse_record(row) for row in raw_rows) if r]

        new_count = store_records(conn, parsed)
        total_seen += len(parsed)
        total_new += new_count

        log.info("Page %d: parsed %d, new %d", page, len(parsed), new_count)

        # If entire page was already known, no point fetching further pages.
        if new_count == 0 and page > 1:
            log.info("No new records on page %d, stopping pagination", page)
            break

    log.info("Done. %d parsed, %d new rows inserted.", total_seen, total_new)
    conn.close()


if __name__ == "__main__":
    main()
