#!/usr/bin/env python3
"""
Regression tests for the announcements_signal/telegram_bot.py filter bugs
(HANDOFF.md §4.3):

    1. log format used '[%(levelname)]' → every log line produced a
       "--- Logging error ---" traceback
    2. the stagnant-price filter read price_ctx['change_pct'], but
       price_context returns 'pct_move' → the move always read 0.0 and
       EVERY net-selling alert was suppressed

No network needed: yfinance and collector are stubbed before import.

    python3 -m unittest test_telegram_bot.py -v     (from announcements_signal/)
"""

import sys
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, val in attrs.items():
        setattr(mod, key, val)
    sys.modules[name] = mod
    return mod


# telegram_bot imports collector (needs cloudscraper) and price_context
# (needs yfinance) — neither is needed for these tests.
_stub("collector", DB_PATH=HERE / "news.db")
_stub("yfinance")

import telegram_bot as tb  # noqa: E402


def _trade(tx="DISPOSAL", shares=5_000_000, score=8):
    return {
        "company_name": "TEST BHD",
        "published_date": "2026-09-20",
        "date_of_change": "2026-09-18",
        "signal_score": score,
        "transaction_type": tx,
        "shares_transacted": shares,
        "consideration": "RM1.000",
        "person_name": "TEST PERSON",
        "nature_of_interest": "Direct",
        "subcategory": "DIRECTOR_S219",
    }


class LogFormatTests(unittest.TestCase):
    def test_levelname_specifier_is_valid(self):
        # '[%(levelname)]' (missing 's') makes Python's logging emit a
        # "--- Logging error ---" traceback for every single line.
        for handler in __import__("logging").getLogger().handlers:
            fmt = getattr(getattr(handler, "formatter", None), "_fmt", "") or ""
            if not fmt:
                continue
            self.assertNotIn("[%(levelname)]", fmt)
            self.assertIn("%(levelname)s", fmt)


class StagnantPriceFilterTests(unittest.TestCase):
    def tearDown(self):
        tb.get_price_context = self._orig_ctx

    def setUp(self):
        self._orig_ctx = tb.get_price_context
        self.trade = [_trade()]                      # 5M DISPOSAL → net selling
        self.ownership, self.streak = [], 1

    def _with_ctx(self, ctx):
        tb.get_price_context = lambda code, day: ctx
        return tb.format_stock_alert("1234", self.trade, self.ownership, self.streak)

    def test_zero_move_plus_selling_is_suppressed(self):
        self.assertIsNone(self._with_ctx({"pct_move": 0.0}))

    def test_real_move_plus_selling_is_sent(self):
        # The old bug: the key was read as 'change_pct', so the move was
        # always 0.0 and this case was wrongly suppressed.
        self.assertIsNotNone(self._with_ctx({"pct_move": -1.5}))

    def test_missing_price_data_fails_open(self):
        # Yahoo unavailable → the alert must NOT be suppressed.
        self.assertIsNotNone(self._with_ctx(None))

    def test_zero_move_plus_buying_is_sent(self):
        self.trade = [_trade(tx="ACQUISITION")]
        self.assertIsNotNone(self._with_ctx({"pct_move": 0.0}))


if __name__ == "__main__":
    unittest.main()
