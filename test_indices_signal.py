#!/usr/bin/env python3
"""Unit tests for indices_signal helpers — no yfinance / network required."""

import sqlite3
import unittest
from datetime import datetime, timezone

from indices_signal.util import (
    UTC_FMT,
    is_market_open,
    migrate_timestamps,
    parse_db_ts,
    to_utc_str,
    utc_now_str,
)


class TimestampTests(unittest.TestCase):
    def test_utc_now_matches_sqlite_shape(self):
        now = utc_now_str()
        datetime.strptime(now, UTC_FMT)
        self.assertNotIn("T", now)
        self.assertNotIn("+", now)

    def test_to_utc_str_strips_offset(self):
        dt = datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(to_utc_str(dt), "2026-08-21 16:00:00")

    def test_parse_legacy_isoformat(self):
        dt = parse_db_ts("2026-08-21T20:00:00+00:00")
        self.assertEqual(dt.strftime(UTC_FMT), "2026-08-21 20:00:00")

    def test_same_day_price_bar_orders_after_signal(self):
        """The bug: space-format bars compared LESS than T-format sent_at."""
        bar = "2026-08-21 20:00:00"
        sent_iso = "2026-08-21T19:00:00+00:00"
        # Broken lexicographic order (what the live DB used to do):
        self.assertLess(bar, sent_iso)
        # After migration both sides are UTC_FMT — bar is after the signal:
        sent = parse_db_ts(sent_iso).strftime(UTC_FMT)
        self.assertGreater(bar, sent)

    def test_migrate_prices_and_signals(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE prices (datetime TEXT)"
        )
        con.execute(
            "CREATE TABLE signals_sent (sent_at TEXT, resolved_at TEXT)"
        )
        con.execute(
            "INSERT INTO prices VALUES (?)",
            ("2026-08-21 16:00:00-04:00",),
        )
        con.execute(
            "INSERT INTO signals_sent VALUES (?, ?)",
            ("2026-08-21T20:05:00+00:00", None),
        )
        n = migrate_timestamps(con)
        self.assertGreaterEqual(n, 2)
        bar = con.execute("SELECT datetime FROM prices").fetchone()[0]
        sent = con.execute("SELECT sent_at FROM signals_sent").fetchone()[0]
        self.assertEqual(bar, "2026-08-21 20:00:00")
        self.assertEqual(sent, "2026-08-21 20:05:00")
        # Second run is a no-op.
        self.assertEqual(migrate_timestamps(con), 0)
        con.close()


class MarketHoursTests(unittest.TestCase):
    def test_us_closed_weekend(self):
        sunday = datetime(2026, 8, 23, 15, 0, tzinfo=timezone.utc)
        saturday = datetime(2026, 8, 22, 15, 0, tzinfo=timezone.utc)
        monday = datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc)
        self.assertFalse(is_market_open("US30", now=sunday))
        self.assertFalse(is_market_open("US100", now=saturday))
        self.assertTrue(is_market_open("US30", now=monday))

    def test_gold_closed_saturday_open_sunday_evening(self):
        saturday = datetime(2026, 8, 22, 15, 0, tzinfo=timezone.utc)
        sunday_morning = datetime(2026, 8, 23, 15, 0, tzinfo=timezone.utc)
        sunday_open = datetime(2026, 8, 23, 22, 30, tzinfo=timezone.utc)
        friday_late = datetime(2026, 8, 21, 21, 30, tzinfo=timezone.utc)
        self.assertFalse(is_market_open("GOLD", now=saturday))
        self.assertFalse(is_market_open("GOLD", now=sunday_morning))
        self.assertTrue(is_market_open("GOLD", now=sunday_open))
        self.assertFalse(is_market_open("GOLD", now=friday_late))

    def test_us_hour_window(self):
        before = datetime(2026, 8, 24, 11, 0, tzinfo=timezone.utc)
        during = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)
        after = datetime(2026, 8, 24, 22, 0, tzinfo=timezone.utc)
        self.assertFalse(is_market_open("US30", now=before))
        self.assertTrue(is_market_open("US30", now=during))
        self.assertFalse(is_market_open("US30", now=after))


if __name__ == "__main__":
    unittest.main()
