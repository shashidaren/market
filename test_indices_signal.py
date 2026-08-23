#!/usr/bin/env python3
"""Unit tests for indices_signal helpers — no yfinance / network required."""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

# Keep DB access out of the repo checkout (track_record etc.).
os.environ.setdefault(
    "INDICES_DB_PATH",
    os.path.join(tempfile.gettempdir(), "test_indices_signal.db"),
)

# signal_engine / telegram_bot use flat imports (cron runs them from
# inside indices_signal/), so put that directory on the path too.
_PKG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "indices_signal")
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from indices_signal.util import (
    UTC_FMT,
    is_market_open,
    migrate_timestamps,
    parse_db_ts,
    to_utc_str,
    utc_now_str,
)

from signal_engine import (  # noqa: E402
    bar_age_hours,
    is_bar_stale,
    rsi_vetoes,
)

from telegram_bot import format_message  # noqa: E402


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


class StaleBarVetoTests(unittest.TestCase):
    """The 2026-08-21 20:00 gold signal: Friday's last 4H bar was
    scored on the Sunday-evening reopen, ~50h after it closed."""

    FRIDAY_BAR = datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc)
    SUNDAY_REOPEN = datetime(2026, 8, 23, 22, 5, tzinfo=timezone.utc)

    def test_friday_bar_is_stale_on_sunday_reopen(self):
        self.assertTrue(
            is_bar_stale(self.FRIDAY_BAR, now=self.SUNDAY_REOPEN)
        )
        # ~46h since the bar CLOSED (started 20:00, closed 24:00 Fri)
        self.assertAlmostEqual(
            bar_age_hours(self.FRIDAY_BAR, now=self.SUNDAY_REOPEN),
            46.08,
            delta=0.1,
        )

    def test_fresh_bar_is_not_stale(self):
        bar = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
        just_after_close = datetime(2026, 8, 24, 12, 30, tzinfo=timezone.utc)
        self.assertFalse(is_bar_stale(bar, now=just_after_close))

    def test_bar_stale_just_past_threshold(self):
        bar = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
        # closed 04:00; max age 6h -> stale strictly after 10:00
        self.assertFalse(
            is_bar_stale(bar, now=datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc))
        )
        self.assertTrue(
            is_bar_stale(bar, now=datetime(2026, 8, 24, 10, 1, tzinfo=timezone.utc))
        )

    def test_naive_and_pandas_timestamps_accepted(self):
        import pandas as pd

        naive = datetime(2026, 8, 21, 20, 0)
        self.assertTrue(is_bar_stale(naive, now=self.SUNDAY_REOPEN))
        ts = pd.Timestamp("2026-08-21 20:00:00", tz="UTC")
        self.assertTrue(is_bar_stale(ts, now=self.SUNDAY_REOPEN))


class RsiVetoTests(unittest.TestCase):
    """The additive score reaches 75 without RSI points, so RSI must
    be an absolute gate: no BUY overbought, no SELL oversold."""

    def test_buy_vetoed_when_overbought(self):
        # The exact reading from the stale gold message.
        self.assertTrue(rsi_vetoes("BUY", 76.6))

    def test_buy_allowed_at_or_below_threshold(self):
        self.assertFalse(rsi_vetoes("BUY", 70.0))
        self.assertFalse(rsi_vetoes("BUY", 55.0))

    def test_sell_vetoed_when_oversold(self):
        self.assertTrue(rsi_vetoes("SELL", 25.0))
        self.assertFalse(rsi_vetoes("SELL", 30.0))
        self.assertFalse(rsi_vetoes("SELL", 45.0))


class MessageFormatTests(unittest.TestCase):
    def _sig(self):
        return {
            "ticker": "GOLD",
            "type": "BUY",
            "price": 4680.60,
            "sl": 4606.46,
            "tp": 4810.34,
            "score": 75,
            "reasons": ["Daily uptrend", "4H EMA20 > EMA50"],
            "rsi": 55.0,
            "adx": 38.4,
            "atr": 37.07,
            "daily_trend": "BULL",
            "bar_time": datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc),
        }

    def test_expiry_placeholder_is_rendered(self):
        """A plain (non-f) string once sent the literal '{expiry}'."""
        msg = format_message(self._sig())
        self.assertNotIn("{expiry}", msg)
        self.assertIn("Act within ~8h", msg)

    def test_bar_time_uses_db_utc_format(self):
        msg = format_message(self._sig())
        self.assertIn("2026-08-21 20:00:00", msg)
        self.assertNotIn("+00:00", msg)


if __name__ == "__main__":
    unittest.main()
