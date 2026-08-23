#!/usr/bin/env python3
"""Unit tests for yahoo_client — no yfinance / network required."""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Point the circuit file at a temp path BEFORE importing the module.
_TMPDIR = tempfile.TemporaryDirectory(prefix="yahoo-circuit-")
os.environ["YAHOO_CIRCUIT_PATH"] = str(Path(_TMPDIR.name) / "circuit.json")
os.environ["YAHOO_COOLDOWN_SECS"] = "60"

import yahoo_client  # noqa: E402


class CircuitTests(unittest.TestCase):
    def setUp(self):
        yahoo_client.reset_circuit()
        yahoo_client._WARNED_OPEN = False
        yahoo_client._PRICE_CACHE.clear()

    def tearDown(self):
        yahoo_client.reset_circuit()

    def test_closed_by_default(self):
        self.assertFalse(yahoo_client.is_circuit_open())
        info = yahoo_client.circuit_info()
        self.assertFalse(info["open"])
        self.assertEqual(info["remaining"], 0.0)

    def test_trip_opens_circuit(self):
        yahoo_client.trip_circuit("test trip", cooldown_secs=30)
        self.assertTrue(yahoo_client.is_circuit_open())
        info = yahoo_client.circuit_info()
        self.assertGreater(info["remaining"], 20)
        self.assertLessEqual(info["remaining"], 30)
        self.assertEqual(info["reason"], "test trip")
        self.assertTrue(yahoo_client.CIRCUIT_PATH.exists())

    def test_reset_closes_circuit(self):
        yahoo_client.trip_circuit("x", cooldown_secs=30)
        yahoo_client.reset_circuit()
        self.assertFalse(yahoo_client.is_circuit_open())
        self.assertFalse(yahoo_client.CIRCUIT_PATH.exists())

    def test_expired_circuit_reads_as_closed(self):
        payload = {
            "open_until": time.time() - 5,
            "reason": "expired",
            "opened_at": "2026-01-01 00:00:00",
        }
        yahoo_client.CIRCUIT_PATH.write_text(json.dumps(payload))
        self.assertFalse(yahoo_client.is_circuit_open())

    def test_corrupt_circuit_file_is_closed(self):
        yahoo_client.CIRCUIT_PATH.write_text("not-json{{{")
        self.assertFalse(yahoo_client.is_circuit_open())

    def test_history_fail_fast_when_open(self):
        yahoo_client.trip_circuit("blocked", cooldown_secs=60)
        # Must not import / call yfinance.
        result = yahoo_client.history("EURUSD=X", interval="1h", period="1d")
        self.assertIsNone(result)

    def test_last_price_fail_fast_when_open(self):
        yahoo_client.trip_circuit("blocked", cooldown_secs=60)
        self.assertIsNone(yahoo_client.last_price("EURUSD=X"))


class RateLimitDetectionTests(unittest.TestCase):
    def test_message_needles(self):
        self.assertTrue(yahoo_client.is_rate_limit_error(RuntimeError("Too Many Requests")))
        self.assertTrue(yahoo_client.is_rate_limit_error(RuntimeError("YFRateLimitError: rate limited")))
        self.assertTrue(yahoo_client.is_rate_limit_error(RuntimeError("HTTP 429 from Yahoo")))
        self.assertFalse(yahoo_client.is_rate_limit_error(RuntimeError("connection reset")))
        self.assertFalse(yahoo_client.is_rate_limit_error(ValueError("no data")))

    def test_response_status_429(self):
        class FakeResp:
            status_code = 429

        class FakeExc(Exception):
            def __init__(self):
                self.response = FakeResp()
                super().__init__("boom")

        self.assertTrue(yahoo_client.is_rate_limit_error(FakeExc()))


class FreshnessTests(unittest.TestCase):
    def test_parse_interval(self):
        self.assertEqual(yahoo_client.parse_interval("1h"), timedelta(hours=1))
        self.assertEqual(yahoo_client.parse_interval("4h"), timedelta(hours=4))
        self.assertEqual(yahoo_client.parse_interval("15m"), timedelta(minutes=15))
        self.assertEqual(yahoo_client.parse_interval("1d"), timedelta(days=1))
        with self.assertRaises(ValueError):
            yahoo_client.parse_interval("1x")
        with self.assertRaises(ValueError):
            yahoo_client.parse_interval("")

    def test_1h_current_closed_bar(self):
        now = datetime(2026, 8, 23, 15, 30, tzinfo=timezone.utc)
        # Last completed 1h bar at 14:00 is current at 15:30.
        self.assertTrue(
            yahoo_client.is_closed_bar_current(
                datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc), "1h", now=now
            )
        )
        # 13:00 is stale at 15:30 (we should have 14:00).
        self.assertFalse(
            yahoo_client.is_closed_bar_current(
                datetime(2026, 8, 23, 13, 0, tzinfo=timezone.utc), "1h", now=now
            )
        )
        # Right after the hour, 13:00 is still stale — we need 14:00.
        just_after = datetime(2026, 8, 23, 15, 0, 10, tzinfo=timezone.utc)
        self.assertFalse(
            yahoo_client.is_closed_bar_current(
                datetime(2026, 8, 23, 13, 0, tzinfo=timezone.utc), "1h", now=just_after
            )
        )
        self.assertTrue(
            yahoo_client.is_closed_bar_current(
                datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc), "1h", now=just_after
            )
        )

    def test_4h_alignment_agnostic(self):
        # Session-aligned 4h bars (13:00 / 17:00). At 16:30 the 13:00 bar
        # is still the last completed one (17:00 hasn't closed).
        now = datetime(2026, 8, 23, 16, 30, tzinfo=timezone.utc)
        self.assertTrue(
            yahoo_client.is_closed_bar_current(
                datetime(2026, 8, 23, 13, 0, tzinfo=timezone.utc), "4h", now=now
            )
        )
        # At 21:05 we need the 17:00 close; 13:00 is stale.
        later = datetime(2026, 8, 23, 21, 5, tzinfo=timezone.utc)
        self.assertFalse(
            yahoo_client.is_closed_bar_current(
                datetime(2026, 8, 23, 13, 0, tzinfo=timezone.utc), "4h", now=later
            )
        )
        self.assertTrue(
            yahoo_client.is_closed_bar_current(
                datetime(2026, 8, 23, 17, 0, tzinfo=timezone.utc), "4h", now=later
            )
        )

    def test_naive_bar_time_assumed_utc(self):
        now = datetime(2026, 8, 23, 15, 30, tzinfo=timezone.utc)
        self.assertTrue(
            yahoo_client.is_closed_bar_current(
                datetime(2026, 8, 23, 14, 0), "1h", now=now
            )
        )


if __name__ == "__main__":
    unittest.main()
