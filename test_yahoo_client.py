#!/usr/bin/env python3
"""
Tests for yahoo_client.py — the shared cross-process Yahoo circuit
breaker. No network needed; every test uses a private state file.

    python3 -m unittest test_yahoo_client.py -v
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from yahoo_client import Circuit, SHARED, COOLDOWN_SECS, EMPTY_STREAK_TRIPS

# Wednesday 2026-09-23 11:00 MYT
T0 = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc).timestamp()


class FakeClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class CircuitTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)                    # created lazily on first write
        self.now = T0
        self.circuit = Circuit(path=self.path, clock=lambda: self.now)

    def tearDown(self):
        for suffix in ("", ".lock"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    # ── basic lifecycle ─────────────────────────────────────────
    def test_starts_closed(self):
        self.assertFalse(self.circuit.is_open())
        self.assertEqual(self.circuit.empty_streak(), 0)
        st = self.circuit.state()
        self.assertFalse(st["is_open"])
        self.assertIsNone(st["until"])

    def test_rate_limit_trips_for_the_full_cooldown(self):
        self.circuit.record_rate_limit("EURUSD=X")
        st = self.circuit.state()
        self.assertTrue(st["is_open"])
        self.assertEqual(st["reason"], "Yahoo rate-limit reply")
        self.assertEqual(st["by"], "EURUSD=X")
        self.assertEqual(st["remaining_secs"], COOLDOWN_SECS)

        self.now += COOLDOWN_SECS - 1
        self.assertTrue(self.circuit.is_open())
        self.now += 1
        self.assertFalse(self.circuit.is_open())   # half-open: calls allowed

    def test_custom_cooldown_secs(self):
        self.circuit.record_rate_limit("X", cooldown_secs=60)
        self.now += 61
        self.assertFalse(self.circuit.is_open())

    def test_empty_streak_trips_at_threshold(self):
        for i in range(EMPTY_STREAK_TRIPS - 1):
            self.circuit.record_empty("EURUSD=X")
            self.assertFalse(self.circuit.is_open(), f"trip after {i + 1}")
        self.circuit.record_empty("EURUSD=X")
        self.assertTrue(self.circuit.is_open())
        st = self.circuit.state()
        self.assertIn("empty replies", st["reason"])

    def test_success_resets_streak_and_closes(self):
        self.circuit.record_rate_limit("X")
        self.now += COOLDOWN_SECS + 1              # half-open
        self.circuit.record_success()
        self.assertFalse(self.circuit.is_open())
        self.assertEqual(self.circuit.empty_streak(), 0)
        self.assertIsNone(self.circuit.state()["reason"])

    def test_success_between_empties_resets_the_streak(self):
        self.circuit.record_empty("A")
        self.circuit.record_empty("B")
        self.circuit.record_success()              # a good reply in between
        self.circuit.record_empty("C")
        self.circuit.record_empty("D")
        self.assertFalse(self.circuit.is_open())   # streak was reset → only 2

    def test_trip_resets_the_streak(self):
        for _ in range(EMPTY_STREAK_TRIPS):
            self.circuit.record_empty("A")
        self.assertEqual(self.circuit.empty_streak(), 0)   # counted down on trip
        self.now += COOLDOWN_SECS + 1
        # one more empty right after reopening must NOT instantly re-trip
        self.circuit.record_empty("A")
        self.assertFalse(self.circuit.is_open())

    # ── state-file robustness ───────────────────────────────────
    def test_corrupt_state_file_is_not_open(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self.assertFalse(self.circuit.is_open())
        self.assertEqual(self.circuit.empty_streak(), 0)
        # and the next write recovers the file
        self.circuit.record_rate_limit("X")
        self.assertTrue(self.circuit.is_open())
        with open(self.path) as fh:
            json.load(fh)                           # valid JSON again

    def test_state_file_is_created_lazily_and_is_json(self):
        self.circuit.record_empty("A")
        self.assertTrue(os.path.exists(self.path))
        with open(self.path) as fh:
            data = json.load(fh)
        self.assertEqual(data["empty_streak"], 1)
        self.assertNotIn("until", data)             # still closed

    def test_shared_default_instance_exists(self):
        self.assertIsInstance(SHARED, Circuit)
        # must never raise even with the real /tmp state file
        self.assertIsInstance(SHARED.is_open(), bool)
        self.assertIsInstance(SHARED.state(), dict)


if __name__ == "__main__":
    unittest.main()
