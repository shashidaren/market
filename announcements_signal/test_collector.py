"""Offline tests for collector retry/budget/heartbeat behaviour. No network."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import cloudscraper  # noqa: F401
except ImportError:  # allow the suite to run without optional HTTP deps
    stub = types.ModuleType("cloudscraper")
    stub.create_scraper = lambda *a, **k: types.SimpleNamespace()
    stub.__version__ = "1.2.71"
    sys.modules["cloudscraper"] = stub

import collector
import collector_state as cs


def row(ann_id: int) -> list:
    return [
        ann_id,
        "01 Nov 2026",
        "<a href='/market/stock_code=1155'>Maybank</a>",
        f"<a href='/announcements/ann_id={ann_id}'>Quarterly Report {ann_id}</a>",
    ]


def payload(*ids: int) -> dict:
    return {"data": [row(i) for i in ids]}


class FakeResponse:
    def __init__(self, status=200, data=None, text="", headers=None):
        self.status_code = status
        self._data = data
        self.text = text or (json.dumps(data) if data is not None else "")
        self.headers = headers or {}

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeScraper:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Factory:
    def __init__(self, scrapers):
        self.scrapers = list(scrapers)
        self.created = 0

    def __call__(self):
        self.created += 1
        return self.scrapers.pop(0)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "news.db"
        self.state_path = cs.state_path_for(self.db)
        self.conn = sqlite3.connect(self.db)
        collector.init_db(self.conn)
        self.clock = [1_000_000.0]
        self.state = cs.empty_state()
        self.sleeps = []

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def budget(self, per_run=5, per_hour=20):
        return cs.RequestBudget(self.state, self.state_path, per_run=per_run,
                                per_hour=per_hour, clock=lambda: self.clock[0])

    def config(self, **kw):
        base = dict(retry_max=5, max_requests=5, hourly_max_requests=20,
                    jitter_min=1.0, jitter_max=3.0, timeout=30, debug=False)
        base.update(kw)
        return collector.CollectorConfig(**base)

    def run_collection(self, scrapers, config=None, budget=None):
        config = config or self.config()
        budget = budget or self.budget(per_run=config.max_requests,
                                       per_hour=config.hourly_max_requests)
        factory = Factory(scrapers)
        result = collector.run_collection(
            self.conn, config, budget,
            scraper_factory=factory, sleep=self.sleeps.append,
            rng=lambda a, b: a,
        )
        return result, factory

    def test_page1_first_then_catchup(self):
        scrapers = [FakeScraper([FakeResponse(data=payload(i))]) for i in (1, 2, 3)]
        result, factory = self.run_collection(scrapers)
        self.assertEqual(result["outcome"], "success")
        self.assertTrue(result["page1_ok"])
        self.assertEqual(result["pages_ok"], [1, 2, 3])
        self.assertEqual(result["requests"], 3)
        self.assertEqual(factory.created, 3)
        pages = [s.calls[0][1]["page"] for s in scrapers]
        self.assertEqual(pages, [1, 2, 3])

    def test_page1_retries_use_fresh_scrapers_and_jitter(self):
        scrapers = [
            FakeScraper([FakeResponse(403, text="Just a moment", headers={"cf-mitigated": "challenge"})]),
            FakeScraper([FakeResponse(403, text="Just a moment", headers={"cf-mitigated": "challenge"})]),
            FakeScraper([FakeResponse(data=payload(1))]),
        ]
        result, factory = self.run_collection(scrapers, self.config(max_requests=3))
        self.assertTrue(result["page1_ok"])
        self.assertEqual(result["page1_attempts"], 3)
        self.assertEqual(factory.created, 3)
        self.assertEqual(self.sleeps, [1.0, 1.0])
        self.assertEqual(result["pages_ok"], [1])  # budget exhausted, no catch-up

    def test_all_page1_failures_exit_failed_and_skip_catchup(self):
        scrapers = [
            FakeScraper([FakeResponse(403, text="Just a moment", headers={"cf-mitigated": "challenge"})])
            for _ in range(5)
        ]
        with self.assertLogs("bursa", level="WARNING") as logs:
            result, factory = self.run_collection(scrapers)
        self.assertEqual(result["outcome"], "failed")
        self.assertFalse(result["page1_ok"])
        self.assertEqual(result["page1_attempts"], 5)
        self.assertEqual(result["requests"], 5)
        self.assertEqual(factory.created, 5)
        failed_lines = [m for m in logs.output if "Failed to fetch" in m]
        self.assertLessEqual(len(failed_lines), 2)
        self.assertTrue(any("Suppressed" in m for m in logs.output))
        self.assertFalse(any("Traceback" in m for m in logs.output))

    def test_empty_page1_is_success(self):
        result, _ = self.run_collection([FakeScraper([FakeResponse(data={"data": []})])],
                                        self.config(max_requests=1))
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["parsed"], 0)
        self.assertEqual(result["new_rows"], 0)

    def test_hourly_budget_shared_across_runs(self):
        path = self.state_path
        state = cs.empty_state()
        for _ in range(4):
            budget = cs.RequestBudget(state, path, per_run=5, per_hour=20,
                                      clock=lambda: self.clock[0])
            for _ in range(5):
                self.assertTrue(budget.reserve())
            state = cs.load_state(path)
        budget = cs.RequestBudget(state, path, per_run=5, per_hour=20,
                                  clock=lambda: self.clock[0])
        self.assertFalse(budget.reserve())
        self.assertEqual(len(cs.load_state(path)["request_times"]), 20)
        self.clock[0] += 3601
        self.assertTrue(budget.reserve())

    def test_budget_exhausted_before_page1_fails(self):
        self.state["request_times"] = [self.clock[0]] * 20
        result, factory = self.run_collection([], self.config(), self.budget())
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["page1_attempts"], 0)
        self.assertEqual(factory.created, 0)

    def test_fetch_page_rejects_200_challenge_and_bad_payload(self):
        scraper = FakeScraper([FakeResponse(200, text="<title>Just a moment...</title>")])
        with self.assertRaises(collector.FetchError) as ctx:
            collector.fetch_page(scraper)
        self.assertEqual(ctx.exception.reason, "cloudflare_challenge")
        scraper = FakeScraper([FakeResponse(200, data={"unexpected": True})])
        with self.assertRaises(collector.FetchError) as ctx:
            collector.fetch_page(scraper)
        self.assertEqual(ctx.exception.reason, "invalid_payload")

    def test_debug_traceback_is_capped(self):
        failure_log = collector.FailureLog(debug=True, repeat_limit=2)
        try:
            raise collector.FetchError(403, "cloudflare_challenge")
        except collector.FetchError as exc:
            with self.assertLogs("bursa", level="DEBUG") as logs:
                for attempt in range(1, 4):
                    failure_log.record(1, attempt, exc)
                failure_log.summary()
        tracebacks = [m for m in logs.output if "Traceback" in m]
        self.assertEqual(len(tracebacks), 1)
        self.assertTrue(any("Suppressed 1" in m for m in logs.output))

    def test_main_heartbeat_success_then_failure_preserves_last_success(self):
        env = {"RETRY_MAX": "1", "ANNOUNCEMENTS_MAX_REQUESTS": "1",
               "ANNOUNCEMENTS_BROWSER_SOLVER": "0", "DEBUG": "0"}
        ok_scraper = FakeScraper([FakeResponse(data=payload(1))])
        with mock.patch.object(collector, "DB_PATH", self.db), \
             mock.patch.object(collector, "_make_scraper", return_value=ok_scraper), \
             mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(collector.main(), 0)
        state = cs.load_state(self.state_path)
        first_success = state["last_success_at"]
        self.assertIsNotNone(first_success)
        self.assertEqual(state["last_run"]["outcome"], "success")

        bad_scraper = FakeScraper([FakeResponse(403, text="Just a moment")])
        with mock.patch.object(collector, "DB_PATH", self.db), \
             mock.patch.object(collector, "_make_scraper", return_value=bad_scraper), \
             mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(collector.main(), 1)
        state = cs.load_state(self.state_path)
        self.assertEqual(state["last_success_at"], first_success)
        self.assertEqual(state["last_run"]["outcome"], "failed")

    def test_config_clamps_hourly_ceiling(self):
        with mock.patch.dict(os.environ, {"ANNOUNCEMENTS_HOURLY_MAX_REQUESTS": "99",
                                          "ANNOUNCEMENTS_MAX_REQUESTS": "99"}, clear=False):
            config = collector.load_config()
        self.assertEqual(config.hourly_max_requests, 20)
        self.assertEqual(config.max_requests, 20)
        with mock.patch.dict(os.environ, {"RETRY_MAX": "zero"}, clear=False):
            with self.assertRaises(ValueError):
                collector.load_config()

    def test_lock_prevents_overlap(self):
        with cs.collector_lock(self.state_path) as first:
            self.assertTrue(first)
            with cs.collector_lock(self.state_path) as second:
                self.assertFalse(second)


if __name__ == "__main__":
    unittest.main()
