"""Offline tests for heartbeat-first preflight and business-day helpers."""

from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import cloudscraper  # noqa: F401
except ImportError:
    stub = types.ModuleType("cloudscraper")
    stub.create_scraper = lambda *a, **k: types.SimpleNamespace()
    stub.__version__ = "1.2.71"
    sys.modules["cloudscraper"] = stub

import collector
import collector_state as cs
import preflight_check as pf


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "news.db"
        self.state_path = cs.state_path_for(self.db)
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE bursa_announcements (ann_id INTEGER PRIMARY KEY, published_date TEXT)")
        con.execute("INSERT INTO bursa_announcements VALUES (1, '2026-10-30')")
        con.execute("CREATE TABLE bursa_insider_details (ann_id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()
        pf.failures = 0

    def tearDown(self):
        self.tmp.cleanup()
        pf.failures = 0

    def write_state(self, *, success_age_min=0, outcome="success"):
        now = datetime.now(timezone.utc)
        state = cs.empty_state()
        state["last_success_at"] = (now - timedelta(minutes=success_age_min)).isoformat()
        state["last_run_at"] = now.isoformat()
        state["last_run"] = {
            "outcome": outcome, "page1_ok": outcome == "success",
            "page1_attempts": 2, "requests": 4, "pages_ok": [1, 2],
            "parsed": 40, "new_rows": 0,
            "error": None if outcome == "success" else "status=403 reason=cloudflare_challenge",
        }
        cs.save_state(self.state_path, state)
        return state

    def check_db(self):
        out = io.StringIO()
        # test_telegram_bot.py replaces sys.modules['collector'] with a stub at
        # import time; make check_db's local import resolve to the real module.
        with mock.patch.dict(sys.modules, {"collector": collector}), \
             mock.patch.object(collector, "DB_PATH", self.db), redirect_stdout(out):
            pf.check_db()
        return out.getvalue()

    def test_quiet_weekend_with_fresh_heartbeat_does_not_fail_or_warn_stale(self):
        self.write_state()
        today_myt = datetime.now(cs.MYT).date().isoformat()
        with mock.patch.dict(os.environ, {"BURSA_HOLIDAYS": today_myt}, clear=False):
            output = self.check_db()
        self.assertEqual(pf.failures, 0)
        self.assertIn("collector heartbeat", output)
        self.assertIn("announcements (secondary)", output)
        self.assertIn("market closed", output)
        self.assertNotIn("stale — is the collector cron running?", output)

    def test_dead_collector_fails_even_with_old_publications(self):
        self.write_state(success_age_min=120)
        output = self.check_db()
        self.assertGreaterEqual(pf.failures, 1)
        self.assertIn("collector heartbeat", output)
        self.assertIn("120 min ago", output)

    def test_failed_last_run_fails_even_if_previous_success_is_recent(self):
        self.write_state(success_age_min=5, outcome="failed")
        self.check_db()
        self.assertGreaterEqual(pf.failures, 1)

    def test_missing_heartbeat_fails(self):
        output = self.check_db()
        self.assertGreaterEqual(pf.failures, 1)
        self.assertIn("no successful page-1 collection", output)

    def test_old_publication_is_secondary_when_heartbeat_fresh(self):
        self.write_state()
        self.check_db()
        self.assertEqual(pf.failures, 0)

    def test_check_deps_prints_cloudscraper_version(self):
        mods = {}
        for name in ("cloudscraper", "requests", "yfinance"):
            mod = types.ModuleType(name)
            mod.__version__ = "1.2.71" if name == "cloudscraper" else "test"
            mods[name] = mod
        out = io.StringIO()
        with mock.patch.dict(sys.modules, mods), redirect_stdout(out):
            pf.check_deps()
        self.assertIn("cloudscraper", out.getvalue())
        self.assertIn("1.2.71", out.getvalue())

    def test_live_403_challenge_is_warn_not_fail(self):
        response = types.SimpleNamespace(status_code=403, headers={"cf-mitigated": "challenge"},
                                         text="<title>Just a moment...</title>")
        scraper = types.SimpleNamespace(get=lambda *a, **k: response)
        mod = types.ModuleType("cloudscraper")
        mod.create_scraper = lambda *a, **k: scraper
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"cloudscraper": mod}), redirect_stdout(out):
            pf.check_bursa()
        self.assertEqual(pf.failures, 0)
        self.assertIn("WARN", out.getvalue())


class BusinessDayTests(unittest.TestCase):
    def test_weekend_and_holiday(self):
        holidays = {date(2026, 11, 9)}
        self.assertFalse(cs.is_business_day(date(2026, 11, 1), holidays))  # Sunday
        self.assertFalse(cs.is_business_day(date(2026, 11, 9), holidays))  # holiday
        self.assertTrue(cs.is_business_day(date(2026, 11, 10), holidays))
        self.assertEqual(cs.previous_business_day(date(2026, 11, 10), holidays), date(2026, 11, 6))

    def test_assess_heartbeat_closed_day_does_not_excuse_dead_collector(self):
        now = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)  # Sunday MYT noon
        state = cs.empty_state()
        state["last_success_at"] = "2026-10-30T16:00:00+00:00"
        state["last_run"] = {"outcome": "success"}
        ok, detail = cs.assess_heartbeat(state, now=now, stale_minutes=60, holidays=set())
        self.assertFalse(ok)
        self.assertIn("Market closed today", detail)

    def test_assess_heartbeat_fresh_on_closed_day(self):
        now = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
        state = cs.empty_state()
        state["last_success_at"] = "2026-11-01T03:30:00+00:00"
        state["last_run"] = {"outcome": "success"}
        ok, detail = cs.assess_heartbeat(state, now=now, stale_minutes=60, holidays=set())
        self.assertTrue(ok)
        self.assertIn("Market closed today", detail)


if __name__ == "__main__":
    unittest.main()
