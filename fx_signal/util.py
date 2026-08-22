#!/usr/bin/env python3
"""
Shared helpers for fx_signal modules.

Centralises the UTC timestamp format used everywhere. CRITICAL detail:
SQLite's datetime('now') returns 'YYYY-MM-DD HH:MM:SS' (space separator,
no timezone offset). Every timestamp we WRITE must use the exact same
format so SQLite string comparisons (signal expiry, dedup windows,
outcome expiry, pruning) behave correctly.

Background (REVIEW.md item #1): the old code stored
'2026-08-22T13:20:00+00:00' (isoformat / 'T' separator). In a string
comparison 'T' (0x54) sorts ABOVE space (0x20), so every stored
same-day timestamp compared as "greater" than datetime('now')
regardless of the actual time — signals never expired during the day,
dedup windows became "whole current UTC day", and outcomes expired
~24h late.
"""

import sqlite3
from datetime import datetime, timezone

# SQLite-compatible UTC format — matches datetime('now') exactly.
UTC_FMT = "%Y-%m-%d %H:%M:%S"

# Columns per table that may hold legacy 'T'/'+00:00' ISO timestamps.
MIGRATION_COLS = {
    "price_signals":    ["bar_time", "collected_at"],
    "signals":          ["generated_at", "expires_at", "delivered_at"],
    "signal_outcomes":  ["generated_at", "resolved_at"],
}


def utc_now_str() -> str:
    """Current UTC time in SQLite-comparable format."""
    return datetime.now(timezone.utc).strftime(UTC_FMT)


def to_db_str(dt: datetime) -> str:
    """Convert any datetime to the SQLite-comparable UTC string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(UTC_FMT)


def parse_db_ts(value) -> datetime | None:
    """
    Parse a stored timestamp — either legacy ISO 'T' format or the
    current space format — into an aware UTC datetime.

    Returns None if the value is missing or unparsable.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def migrate_timestamps(conn: sqlite3.Connection) -> int:
    """
    Idempotent one-time migration: convert any legacy ISO timestamps
    ('2026-08-22T13:20:00+00:00') to the SQLite-comparable space format
    ('2026-08-22 13:20:00'). Safe to call on every startup — rows already
    in the space format never match the LIKE '%T%' filter.

    Returns the number of rows converted.
    """
    total = 0
    for table, cols in MIGRATION_COLS.items():
        for col in cols:
            try:
                rows = conn.execute(
                    f"SELECT rowid, {col} FROM {table} "
                    f"WHERE {col} LIKE '%T%'"
                ).fetchall()
            except sqlite3.OperationalError:
                continue  # table or column missing — nothing to migrate
            updates = []
            for rowid, val in rows:
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
