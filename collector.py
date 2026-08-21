#!/usr/bin/env python3
"""
Market news collector - polls RSS feeds, dedups by URL hash, stores raw
headlines in SQLite for the scoring stage to pick up later.

Run manually to test, or via cron every 30 min:
    */30 * * * * /usr/bin/python3 /opt/webscrap/collector.py >> /var/log/webscrap/collector.log 2>&1
"""

import feedparser
import hashlib
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "news.db"
LOG_LEVEL = logging.INFO

# Source name -> RSS feed URL. Add/remove here as the source list gets
# refined - this is the thing we're validating first.
#
# Confirmed working (2026-08-08, tested live from the LXC):
#   Borneo Post - Business, Financial Times - International
#
# Dropped: The Edge Markets native RSS (both endpoints) returns
# "404 No input file specified" - a PHP backend error indicating the
# feed-generation script no longer exists on their end. Not a config
# issue on our side - they appear to have discontinued public RSS.
FEEDS = {
    "Borneo Post - Business": "https://www.theborneopost.com/feed/",
    "Financial Times - International": "https://www.ft.com/rss/home/international",
    # Workaround for Edge Markets coverage via Google News RSS, scoped
    # to their domain. UNTESTED - verify this returns real entries
    # before relying on it; Google News RSS sometimes rate-limits or
    # changes format without notice.
    "The Edge Markets (via Google News)": "https://news.google.com/rss/search?q=site:theedgemarkets.com+when:1d&hl=en-MY&gl=MY&ceid=MY:en",
}

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("collector")


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_headlines (
            id           TEXT PRIMARY KEY,
            source       TEXT NOT NULL,
            title        TEXT NOT NULL,
            snippet      TEXT,
            url          TEXT NOT NULL,
            published_at TEXT,
            collected_at TEXT NOT NULL,
            scored       INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_unscored ON raw_headlines(scored)"
    )
    conn.commit()


def make_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def strip_html(raw: str) -> str:
    """Removes HTML tags and their attributes entirely (not just the tag
    names), so encoded URLs sitting inside href attributes - e.g. Google
    News RSS descriptions, which are just an <a href="...encoded...">
    wrapper - never leak into the scored text. A base64-ish URL can
    coincidentally contain letter sequences that look like a ticker or
    keyword, causing false positive matches downstream."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_published(entry) -> str | None:
    """Best-effort extraction of a published timestamp as ISO string."""
    for field in ("published_parsed", "updated_parsed"):
        value = getattr(entry, field, None)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc).isoformat()
    return None


def fetch_feed(source: str, url: str) -> list[dict]:
    parsed = feedparser.parse(url)
    if parsed.bozo:
        log.warning("Feed parse issue for %s: %s", source, parsed.bozo_exception)

    items = []
    for entry in parsed.entries:
        link = getattr(entry, "link", None)
        title = getattr(entry, "title", None)
        if not link or not title:
            continue
        items.append(
            {
                "id": make_id(link),
                "source": source,
                "title": title.strip(),
                "snippet": strip_html(getattr(entry, "summary", ""))[:500],
                "url": link,
                "published_at": parse_published(entry),
            }
        )
    return items


def store_items(conn: sqlite3.Connection, items: list[dict]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for item in items:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO raw_headlines
                (id, source, title, snippet, url, published_at, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                item["source"],
                item["title"],
                item["snippet"],
                item["url"],
                item["published_at"],
                now,
            ),
        )
        if cur.rowcount:
            inserted += 1
    conn.commit()
    return inserted


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    total_seen = 0
    total_new = 0

    for source, url in FEEDS.items():
        try:
            items = fetch_feed(source, url)
        except Exception:
            log.exception("Failed to fetch feed: %s", source)
            continue

        new_count = store_items(conn, items)
        total_seen += len(items)
        total_new += new_count
        log.info("%s: %d entries, %d new", source, len(items), new_count)

    log.info("Done. %d entries seen, %d new rows inserted.", total_seen, total_new)
    conn.close()


if __name__ == "__main__":
    main()
