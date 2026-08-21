#!/usr/bin/env python3
"""
One-shot backfill script - pulls historical announcements from Bursa
until we hit records older than DAYS_TO_BACKFILL.

Run this ONCE after setting up collector.py. After that, cron handles
incremental updates.

    python3 backfill.py

Uses the same storage layer as collector.py, so it's safe to run
multiple times - INSERT OR IGNORE dedups on ann_id.
"""

import logging
import sys
import time
from datetime import date, datetime, timedelta

import cloudscraper

from collector import (
    fetch_page,
    parse_record,
    store_records,
    init_db,
    DB_PATH,
    PER_PAGE,
)
import sqlite3

DAYS_TO_BACKFILL = 7
MAX_PAGES = 100  # safety limit - stops runaway pagination
SLEEP_BETWEEN_PAGES = 1.0  # be polite to Bursa API

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("backfill")


def main() -> None:
    cutoff_date = date.today() - timedelta(days=DAYS_TO_BACKFILL)
    log.info("Backfilling announcements newer than %s", cutoff_date)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    scraper = cloudscraper.create_scraper()
    total_seen = 0
    total_new = 0

    for page in range(1, MAX_PAGES + 1):
        try:
            data = fetch_page(scraper, page=page, per_page=PER_PAGE)
        except Exception:
            log.exception("Failed to fetch page %d, stopping", page)
            break

        raw_rows = data.get("data", [])
        if not raw_rows:
            log.info("Empty page %d, done", page)
            break

        parsed = [r for r in (parse_record(row) for row in raw_rows) if r]

        # Check the oldest record on this page - if it's older than
        # our cutoff, we've backfilled enough.
        oldest_on_page = None
        for r in parsed:
            if r["published_date"]:
                d = datetime.strptime(r["published_date"], "%Y-%m-%d").date()
                if oldest_on_page is None or d < oldest_on_page:
                    oldest_on_page = d

        new_count = store_records(conn, parsed)
        total_seen += len(parsed)
        total_new += new_count

        log.info(
            "Page %d: parsed %d, new %d, oldest %s",
            page, len(parsed), new_count, oldest_on_page,
        )

        if oldest_on_page and oldest_on_page < cutoff_date:
            log.info(
                "Reached cutoff (oldest %s < cutoff %s), stopping",
                oldest_on_page, cutoff_date,
            )
            break

        time.sleep(SLEEP_BETWEEN_PAGES)

    log.info("Backfill done. %d parsed, %d new rows inserted.", total_seen, total_new)
    conn.close()


if __name__ == "__main__":
    main()
