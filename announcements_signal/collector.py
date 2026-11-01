#!/usr/bin/env python3
"""
Bursa Malaysia announcements collector.

Fetches company announcements from the public API at:
    https://www.bursamalaysia.com/api/v1/announcements/search

The API sits behind a Cloudflare managed JS challenge.  cloudscraper 1.2.71
is the latest release and only solves it intermittently, so this collector
retries page 1 with fresh scraper instances inside a small, shared request
budget.  Pages 2-3 are best-effort catch-up and never jeopardise page 1.

    */10 * * * * cd /opt/market/announcements_signal && \
        /usr/bin/python3 collector.py >> /var/log/webscrap-bursa-collector.log 2>&1

Exit codes: 0 = page 1 collected (or another run holds the lock),
1 = page 1 failed, 2 = bad configuration.  A successful empty page still
refreshes the heartbeat; zero new rows is not a failure.

Layer C: fetch + parse + categorize + store in SQLite.  INSERT OR IGNORE on
ann_id keeps re-runs idempotent.  No existing table is rewritten.
"""

from __future__ import annotations

import logging
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import cloudscraper

from announcements_config import categorize
from collector_state import (
    RequestBudget,
    collector_lock,
    load_state,
    save_state,
    state_path_for,
)

API_URL = "https://www.bursamalaysia.com/api/v1/announcements/search"
BASE_URL = "https://www.bursamalaysia.com"

# Shared DB with news collector - simpler for combined dashboard.
DB_PATH = Path(__file__).parent.parent / "news.db"

PAGES_TO_FETCH = 3
PER_PAGE = 20

# Safety ceiling: the old cron used 3 requests/run = 18/hour.  The default
# rolling cap is 20/hour (+2); operators may lower it, not raise it past 20.
HOURLY_HARD_CAP = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bursa")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class CollectorConfig:
    retry_max: int = 5
    max_requests: int = 5
    hourly_max_requests: int = HOURLY_HARD_CAP
    jitter_min: float = 1.0
    jitter_max: float = 3.0
    timeout: int = 30
    debug: bool = False
    browser_solver: bool = False
    browser_timeout: int = 45
    stale_minutes: float = 60.0


def load_config() -> CollectorConfig:
    retry_max = _env_int("RETRY_MAX", 5)
    hourly = _env_int("ANNOUNCEMENTS_HOURLY_MAX_REQUESTS", HOURLY_HARD_CAP)
    if hourly > HOURLY_HARD_CAP:
        log.warning("ANNOUNCEMENTS_HOURLY_MAX_REQUESTS clamped to %d", HOURLY_HARD_CAP)
        hourly = HOURLY_HARD_CAP
    max_requests = min(_env_int("ANNOUNCEMENTS_MAX_REQUESTS", 5), hourly)
    jitter_min = _env_float("ANNOUNCEMENTS_JITTER_MIN", 1.0)
    jitter_max = _env_float("ANNOUNCEMENTS_JITTER_MAX", 3.0)
    if jitter_max < jitter_min:
        raise ValueError("ANNOUNCEMENTS_JITTER_MAX must be >= ANNOUNCEMENTS_JITTER_MIN")
    return CollectorConfig(
        retry_max=retry_max,
        max_requests=max_requests,
        hourly_max_requests=hourly,
        jitter_min=jitter_min,
        jitter_max=jitter_max,
        timeout=_env_int("ANNOUNCEMENTS_TIMEOUT", 30),
        debug=_truthy(os.environ.get("DEBUG")),
        browser_solver=_truthy(os.environ.get("ANNOUNCEMENTS_BROWSER_SOLVER")),
        browser_timeout=_env_int("ANNOUNCEMENTS_BROWSER_TIMEOUT", 45),
        stale_minutes=_env_float("ANNOUNCEMENTS_STALE_MINUTES", 60.0, minimum=1.0),
    )


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

class FetchError(Exception):
    """One-line fetch failure with a stable status/reason for log dedupe."""

    def __init__(self, status: int | str, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(f"status={status} reason={reason}")


def _looks_like_challenge(text: str) -> bool:
    sample = (text or "")[:1000].lower()
    return "just a moment" in sample or "cf-chl" in sample or "cloudflare" in sample


def fetch_page(scraper, page: int = 1, per_page: int = 20, timeout: int = 30) -> dict:
    """Fetch one API page and validate that it is really announcement JSON.

    A 200 HTML challenge page is treated as a failure, not an empty success.
    Raises FetchError (or the underlying requests exception on transport errors).
    """
    response = scraper.get(
        API_URL,
        params={"ann_type": "company", "per_page": per_page, "page": page},
        timeout=timeout,
    )
    status = response.status_code
    if status != 200:
        cf = (response.headers or {}).get("cf-mitigated", "")
        reason = "cloudflare_challenge" if cf == "challenge" or _looks_like_challenge(response.text) else f"http_{status}"
        raise FetchError(status, reason)
    try:
        data = response.json()
    except ValueError:
        reason = "cloudflare_challenge" if _looks_like_challenge(response.text) else "invalid_json"
        raise FetchError(status, reason) from None
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise FetchError(status, "invalid_payload")
    return data


def _make_scraper(browser_session: dict | None = None):
    """Fresh cloudscraper instance; optionally reuse a solved browser cookie."""
    scraper = cloudscraper.create_scraper()
    if browser_session:
        ua = browser_session.get("user_agent")
        if ua:
            scraper.headers["User-Agent"] = ua
        for name, value in (browser_session.get("cookies") or {}).items():
            scraper.cookies.set(name, value, domain=".bursamalaysia.com", path="/")
    return scraper


class FailureLog:
    """One line per failure, capped per identical (page, status, reason)."""

    def __init__(self, debug: bool = False, repeat_limit: int = 2):
        self.debug = debug
        self.repeat_limit = max(1, repeat_limit)
        self.counts: dict[tuple, int] = {}
        self.suppressed = 0

    def record(self, page: int, attempt: int, exc: Exception) -> None:
        status = getattr(exc, "status", None)
        reason = getattr(exc, "reason", None)
        if status is None:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", "transport")
        if reason is None:
            reason = type(exc).__name__
        key = (page, status, reason)
        count = self.counts.get(key, 0) + 1
        self.counts[key] = count
        if count <= self.repeat_limit:
            log.warning("Failed to fetch page=%d attempt=%d status=%s reason=%s",
                        page, attempt, status, reason)
            if self.debug and count == 1:
                log.debug("Traceback for page=%d status=%s reason=%s", page, status, reason,
                          exc_info=exc)
        else:
            self.suppressed += 1

    def summary(self) -> None:
        if self.suppressed:
            log.warning("Suppressed %d repeated fetch failure line(s); DEBUG=1 for first traceback per kind",
                        self.suppressed)


def _process_page(conn: sqlite3.Connection, data: dict) -> tuple[int, int]:
    raw_rows = data.get("data", [])
    parsed = [r for r in (parse_record(row) for row in raw_rows) if r]
    new_count = store_records(conn, parsed)
    return len(parsed), new_count


def run_collection(conn: sqlite3.Connection, config: CollectorConfig,
                   budget: RequestBudget, *, scraper_factory=None,
                   sleep=time.sleep, rng=random.uniform,
                   browser_session: dict | None = None,
                   failure_log: FailureLog | None = None) -> dict:
    """Page 1 first with bounded retries; pages 2-3 only after page 1 succeeds."""
    scraper_factory = scraper_factory or (lambda: _make_scraper(browser_session))
    failure_log = failure_log or FailureLog(config.debug)
    page1_ok = False
    page1_attempts = 0
    pages_ok: list[int] = []
    total_seen = 0
    total_new = 0
    last_error: str | None = None

    for attempt in range(1, config.retry_max + 1):
        if not budget.reserve():
            last_error = last_error or "request budget exhausted"
            break
        page1_attempts = attempt
        try:
            scraper = scraper_factory()
            data = fetch_page(scraper, page=1, per_page=PER_PAGE, timeout=config.timeout)
            seen, new = _process_page(conn, data)
            total_seen += seen
            total_new += new
            page1_ok = True
            pages_ok.append(1)
            log.info("Page 1: parsed %d, new %d (attempt %d)", seen, new, attempt)
            break
        except Exception as exc:
            last_error = str(exc)
            failure_log.record(1, attempt, exc)
            if attempt < config.retry_max and budget.remaining() > 0:
                sleep(rng(config.jitter_min, config.jitter_max))

    if page1_ok:
        for page in range(2, PAGES_TO_FETCH + 1):
            if not budget.reserve():
                break
            try:
                scraper = scraper_factory()
                data = fetch_page(scraper, page=page, per_page=PER_PAGE, timeout=config.timeout)
                seen, new = _process_page(conn, data)
                total_seen += seen
                total_new += new
                pages_ok.append(page)
                log.info("Page %d: parsed %d, new %d", page, seen, new)
                if new == 0:
                    log.info("No new records on page %d, stopping pagination", page)
                    break
            except Exception as exc:
                failure_log.record(page, 1, exc)
                continue

    failure_log.summary()
    return {
        "outcome": "success" if page1_ok else "failed",
        "page1_ok": page1_ok,
        "page1_attempts": page1_attempts,
        "requests": budget.run_requests,
        "pages_ok": pages_ok,
        "parsed": total_seen,
        "new_rows": total_new,
        "error": None if page1_ok else (last_error or "page 1 unavailable"),
    }


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

def main() -> int:
    try:
        config = load_config()
    except ValueError as exc:
        log.error("Invalid collector configuration: %s", exc)
        return 2
    if config.debug:
        log.setLevel(logging.DEBUG)

    path = state_path_for(DB_PATH)
    with collector_lock(path) as acquired:
        if not acquired:
            log.info("Skipping: another collector run holds the lock")
            return 0
        state = load_state(path)
        budget = RequestBudget(state, path, per_run=config.max_requests,
                               per_hour=config.hourly_max_requests)
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            init_db(conn)
            browser_session = None
            if config.browser_solver:
                try:
                    from browser_solver import solve_challenge
                    browser_session = solve_challenge(timeout=config.browser_timeout,
                                                      reserve=budget.reserve)
                except Exception as exc:
                    log.warning("Browser solver unavailable: %s; using cloudscraper", exc)
            result = run_collection(conn, config, budget,
                                    browser_session=browser_session)
            now = datetime.now(timezone.utc).isoformat()
            state["last_run_at"] = now
            state["last_run"] = result
            if result["page1_ok"]:
                state["last_success_at"] = now
            save_state(path, state)
            log.info("Done. outcome=%s parsed=%d new=%d requests=%d pages_ok=%s",
                     result["outcome"], result["parsed"], result["new_rows"],
                     result["requests"], result["pages_ok"])
            return 0 if result["page1_ok"] else 1
        except Exception as exc:
            log.error("Collector run failed: %s: %s", type(exc).__name__, exc)
            if config.debug:
                log.exception("Collector traceback")
            now = datetime.now(timezone.utc).isoformat()
            state["last_run_at"] = now
            state["last_run"] = {"outcome": "failed", "page1_ok": False,
                                 "page1_attempts": 0, "requests": budget.run_requests,
                                 "pages_ok": [], "parsed": 0, "new_rows": 0,
                                 "error": f"{type(exc).__name__}: {exc}"}
            try:
                save_state(path, state)
            except OSError:
                pass
            return 1
        finally:
            if conn is not None:
                conn.close()


if __name__ == "__main__":
    sys.exit(main())
