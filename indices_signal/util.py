#!/usr/bin/env python3
"""
Shared helpers for indices_signal.

Same timestamp rule as fx_signal/util.py: everything we WRITE must be
'YYYY-MM-DD HH:MM:SS' UTC so SQLite datetime('now') comparisons work.

Background: log_signal used to store isoformat ('2026-08-22T13:20:00+00:00')
and the collector stored pandas Timestamp strings (often with an offset).
outcome_tracker then compared those to datetime('now') ('2026-08-22 13:20:00').
' T' > ' ', so same-day sent_at never expired and same-day 4H bars never
counted as "after the signal" — TP/SL hits on the signal day were missed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

UTC_FMT = "%Y-%m-%d %H:%M:%S"

MIGRATION_COLS = {
    "prices":       ["datetime"],
    "signals_sent": ["sent_at", "resolved_at"],
}


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime(UTC_FMT)


def to_utc_str(bar_time) -> str:
    """Normalise a pandas/datetime timestamp to UTC_FMT."""
    if hasattr(bar_time, "to_pydatetime"):
        bar_time = bar_time.to_pydatetime()
    if isinstance(bar_time, str):
        parsed = parse_db_ts(bar_time)
        if parsed is None:
            return utc_now_str()
        return parsed.strftime(UTC_FMT)
    if hasattr(bar_time, "tzinfo") and bar_time.tzinfo is not None:
        bar_time = bar_time.astimezone(timezone.utc)
    elif hasattr(bar_time, "replace"):
        bar_time = bar_time.replace(tzinfo=timezone.utc)
    else:
        bar_time = datetime.now(timezone.utc)
    return bar_time.strftime(UTC_FMT)


def parse_db_ts(value) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.strptime(raw, UTC_FMT)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _already_utc_fmt(value: str) -> bool:
    try:
        datetime.strptime(value, UTC_FMT)
        return True
    except (TypeError, ValueError):
        return False


def migrate_timestamps(conn: sqlite3.Connection) -> int:
    """
    Idempotent: convert any non-UTC_FMT timestamp to UTC_FMT.
    Safe to call on every startup.
    """
    total = 0
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for table, cols in MIGRATION_COLS.items():
        if table not in tables:
            continue
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col not in existing:
                continue
            try:
                rows = conn.execute(
                    f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL"
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            updates = []
            for rowid, val in rows:
                if _already_utc_fmt(str(val)):
                    continue
                parsed = parse_db_ts(val)
                if parsed is not None:
                    updates.append((parsed.strftime(UTC_FMT), rowid))
            if updates:
                conn.executemany(
                    f"UPDATE {table} SET {col} = ? WHERE rowid = ?", updates
                )
                total += len(updates)
    if total:
        conn.commit()
    return total


# ------------------------------------------------------------------
# Market hours (weekday-aware — the old hour-only filter treated
# Sunday 15:00 UTC as "US30 open")
# ------------------------------------------------------------------
def is_market_open(ticker_key: str, now: datetime | None = None) -> bool:
    """
    Coarse UTC session filter.

    US30 / US100: Mon–Fri, 12:00–21:00 UTC.
    GOLD: Sun 22:00 UTC → Fri 21:00 UTC (COMEX almost-24h session).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    dow = now.weekday()  # 0=Mon … 6=Sun
    hour = now.hour

    if ticker_key in ("US30", "US100"):
        if dow >= 5:
            return False
        return 12 <= hour <= 21

    if ticker_key == "GOLD":
        if dow == 5:                          # Saturday
            return False
        if dow == 4 and hour >= 21:           # Friday after 21:00
            return False
        if dow == 6 and hour < 22:            # Sunday before 22:00
            return False
        return True

    return False
