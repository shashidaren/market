#!/usr/bin/env python3
"""
Tests for insider_movers.py and the /movers routes. No network needed —
Yahoo is replaced by a fake fetcher and a fake clock.

    python3 -m unittest test_insider_movers.py -v
"""

import importlib.util
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import insider_movers as im

# Thursday 2026-09-24 11:00 MYT → inside Bursa hours
MARKET_EPOCH = datetime(2026, 9, 24, 3, 0, tzinfo=timezone.utc).timestamp()
# Saturday → outside Bursa hours
WEEKEND_EPOCH = datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc).timestamp()


def utc_day(offset: int) -> str:
    """ISO date `offset` days before today (UTC, like SQLite's date('now'))."""
    return (datetime.now(timezone.utc).date() - timedelta(days=offset)).isoformat()


SCHEMA = """
CREATE TABLE bursa_announcements (
    ann_id INTEGER PRIMARY KEY, stock_code TEXT, company_name TEXT,
    title TEXT NOT NULL, category TEXT, subcategory TEXT, priority INTEGER,
    published_date TEXT, url TEXT, collected_at TEXT NOT NULL);
CREATE TABLE bursa_insider_details (
    ann_id INTEGER PRIMARY KEY, stock_code TEXT, company_name TEXT,
    published_date TEXT, person_name TEXT, transaction_type TEXT,
    nature_of_interest TEXT, shares_transacted INTEGER, consideration TEXT,
    price_per_share REAL, direct_units_after INTEGER, direct_pct_after REAL,
    indirect_units_after INTEGER, indirect_pct_after REAL, date_of_change TEXT,
    date_of_notice TEXT, circumstances TEXT, parsed_at TEXT,
    signal_score INTEGER DEFAULT 0, alert_ready INTEGER DEFAULT 0,
    delivered INTEGER DEFAULT 0);
"""


class DB:
    """Tiny helper to build an insider DB in a temp file."""

    def __init__(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self.next_id = 1000

    def add(self, code, *, score=8, pub=1, trade=None, person="TAN AH KOW",
            tx="ACQUISITION", shares=500_000, consideration="RM0.870", pps=0.87,
            subcat="DIRECTOR_S219", circumstances="Acquisition via open market",
            company=None):
        self.next_id += 1
        pub_d = utc_day(pub)
        trade_d = utc_day(trade if trade is not None else pub + 2)
        self.conn.execute(
            "INSERT INTO bursa_announcements VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.next_id, code, company or f"CO {code} BHD", "Changes in ...",
             "INSIDER_TRADE", subcat, 1, pub_d, f"https://example/{self.next_id}", "now"))
        self.conn.execute(
            """INSERT INTO bursa_insider_details
               (ann_id, stock_code, company_name, published_date, person_name,
                transaction_type, shares_transacted, consideration, price_per_share,
                date_of_change, circumstances, signal_score, parsed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.next_id, code, company or f"CO {code} BHD", pub_d, person, tx, shares,
             consideration, pps, trade_d, circumstances, score, "now"))
        self.conn.commit()
        return self.next_id

    def close(self):
        self.conn.close()
        os.unlink(self.path)


def make_bars(n=120, start=1.00, drift=0.002, vol=100_000, last_vol=None, end=None):
    """n weekday bars ending at `end` (default: today) — deterministic."""
    end = end or datetime.now(timezone.utc).date()
    days = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    bars, price = [], start
    for i, day in enumerate(days):
        o = price
        price = round(price * (1 + drift), 4)
        bars.append({"t": day.isoformat(), "o": o, "h": max(o, price) * 1.01,
                     "l": min(o, price) * 0.99, "c": price, "v": vol})
    if last_vol is not None:
        for b in bars[-5:]:
            b["v"] = last_vol
    return bars


class FakeClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


# ─── Pure helpers ──────────────────────────────────────────────────

class HelperTests(unittest.TestCase):
    def test_yahoo_symbol(self):
        self.assertEqual(im.yahoo_symbol("5352"), "5352.KL")
        self.assertEqual(im.yahoo_symbol("208"), "0208.KL")
        self.assertEqual(im.yahoo_symbol(" 0166 "), "0166.KL")

    def test_classify_method(self):
        cases = {
            "Acquisition of shares via open market": "open-mkt",
            "Disposal of shares via off-market transaction": "off-mkt",
            "Deemed interest via spouse's acquisition in the open market": "open-mkt",
            "Exercise of ESOS options": "esos",
            "Vesting of shares under the Share Grant Plan": "esos",
            "Subscription of Rights Issue shares": "corp-action",
            "Transfer of shares to family trust": "transfer",
            "Disposal of shares by Employees Provident Fund Board": "other",
            "": "unknown",
            None: "unknown",
        }
        for text, expected in cases.items():
            self.assertEqual(im.classify_method(text), expected, text)

    def test_parse_trade_price(self):
        self.assertEqual(im.parse_trade_price("RM0.870", 0.87), 0.87)
        self.assertEqual(im.parse_trade_price("RM0.870 per share", None), 0.87)
        self.assertEqual(im.parse_trade_price("RM0.85 - RM0.90", None), 0.875)
        self.assertEqual(im.parse_trade_price("RM1,250,000.00", None), 1250000.0)  # sanity-filtered later
        self.assertIsNone(im.parse_trade_price("RM0.85, RM0.86 and RM0.90", None))
        self.assertIsNone(im.parse_trade_price(None, None))
        self.assertIsNone(im.parse_trade_price("-", None))

    def test_bursa_session_open(self):
        utc = timezone.utc
        self.assertTrue(im.bursa_session_open(datetime(2026, 9, 24, 3, 0, tzinfo=utc)))    # Thu 11:00 MYT
        self.assertFalse(im.bursa_session_open(datetime(2026, 9, 24, 12, 0, tzinfo=utc)))  # Thu 20:00 MYT
        self.assertFalse(im.bursa_session_open(datetime(2026, 9, 26, 3, 0, tzinfo=utc)))   # Saturday

    def test_parse_params_clamps(self):
        self.assertEqual(im.parse_params({}), (7, 5, "score"))
        self.assertEqual(im.parse_params({"days": "999", "limit": "50", "rank": "shares"}), (90, 10, "shares"))
        self.assertEqual(im.parse_params({"days": "x", "limit": "0", "rank": "bogus"}), (7, 1, "score"))


# ─── Insider side ──────────────────────────────────────────────────

class LeaderTests(unittest.TestCase):
    def setUp(self):
        self.db = DB()

    def tearDown(self):
        self.db.close()

    def test_dedupe_prefers_s219_and_keeps_distinct_trades(self):
        db = self.db
        # Same trade filed as S219 (with price) and S138 (without)
        db.add("5352", subcat="SUBSTANTIAL_S138", consideration=None, pps=None, person="Tan  Ah Kow")
        db.add("5352", subcat="DIRECTOR_S219")
        # A genuinely different trade the same day (different size)
        db.add("5352", shares=200_000)
        rows = im.fetch_insider_rows(db.conn, 7)
        trades = im.dedupe_trades(rows)
        self.assertEqual(len(trades), 2)
        big = next(t for t in trades if t["shares"] == 500_000)
        self.assertEqual(big["filing"], "D")
        self.assertEqual(big["price"], 0.87)
        self.assertEqual(big["method"], "open-mkt")
        self.assertEqual(big["lag_days"], 2)

    def test_ranking_modes_limit_and_filters(self):
        db = self.db
        # AAAA: highest score, 1 filing
        db.add("1001", score=14, shares=300_000)
        # BBBB: most filings
        for i in range(4):
            db.add("1002", score=8, pub=1 + i, shares=150_000 + i)
        # CCCC: most shares, low score, net seller
        db.add("1003", score=7, tx="DISPOSAL", shares=9_000_000)
        # Warrant — always excluded
        db.add("1004WA", score=20, shares=50_000_000)
        # Unscored stock — excluded when others are scored
        db.add("1005", score=0, shares=99_000_000)
        # Outside the 7-day window
        db.add("1006", score=30, pub=20)

        by_score = im.load_leaders(db.conn, days=7, limit=5, rank="score")
        self.assertEqual([s["stock_code"] for s in by_score["stocks"]], ["1001", "1002", "1003"])
        self.assertEqual(by_score["eligible"], 3)
        self.assertTrue(by_score["scored"])

        by_filings = im.load_leaders(db.conn, days=7, rank="filings")
        self.assertEqual(by_filings["stocks"][0]["stock_code"], "1002")
        self.assertEqual(by_filings["stocks"][0]["n_filings"], 4)

        by_shares = im.load_leaders(db.conn, days=7, rank="shares")
        top = by_shares["stocks"][0]
        self.assertEqual(top["stock_code"], "1003")
        self.assertEqual(top["direction"], "sell")
        self.assertEqual(top["net_shares"], -9_000_000)

        self.assertEqual(len(im.load_leaders(db.conn, days=7, limit=2)["stocks"]), 2)
        # 30-day window picks up the old, high-score filing
        self.assertEqual(im.load_leaders(db.conn, days=30)["stocks"][0]["stock_code"], "1006")

    def test_fallback_when_nothing_scored(self):
        self.db.add("2001", score=0, shares=100_000)
        self.db.add("2002", score=0, shares=900_000)
        res = im.load_leaders(self.db.conn, days=7, rank="shares")
        self.assertFalse(res["scored"])
        self.assertEqual([s["stock_code"] for s in res["stocks"]], ["2002", "2001"])

    def test_missing_tables_is_a_clean_error(self):
        conn = sqlite3.connect(":memory:")
        res = im.load_leaders(conn)
        self.assertEqual(res["stocks"], [])
        self.assertIn("insider tables not available", res["error"])

    def test_old_db_without_signal_score_column(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA.replace(
            ",\n    signal_score INTEGER DEFAULT 0, alert_ready INTEGER DEFAULT 0,\n    delivered INTEGER DEFAULT 0", ""))
        conn.execute("INSERT INTO bursa_announcements VALUES (1,'3001','X','t','INSIDER_TRADE',"
                     "'DIRECTOR_S219',1,?,NULL,'now')", (utc_day(1),))
        conn.execute("INSERT INTO bursa_insider_details (ann_id, stock_code, published_date, "
                     "transaction_type, shares_transacted) VALUES (1,'3001',?,'ACQUISITION',100)",
                     (utc_day(1),))
        res = im.load_leaders(conn)
        self.assertEqual(res["stocks"][0]["stock_code"], "3001")
        self.assertFalse(res["scored"])


# ─── Metrics ───────────────────────────────────────────────────────

class MetricTests(unittest.TestCase):
    def setUp(self):
        self.bars = make_bars(n=120, start=1.00, drift=0.002, vol=100_000, last_vol=300_000)
        self.first = self.bars[-10]["t"]
        self.latest = self.bars[-4]["t"]

    def summary(self, direction="buy", trades=None):
        trades = trades if trades is not None else [
            {"side": "buy", "shares": 1_000_000, "price": self.bars[-10]["c"], "trade_date": self.first},
            {"side": "buy", "shares": 1_000_000, "price": self.bars[-4]["c"], "trade_date": self.latest},
            # A 'total consideration' parsed as a price → must be ignored
            {"side": "buy", "shares": 5_000_000, "price": 1_250_000.0, "trade_date": self.latest},
        ]
        return {"first_trade_date": self.first, "last_trade_date": self.latest,
                "direction": direction, "net_shares": 2_000_000, "trades": trades}

    def test_core_metrics(self):
        m = im.compute_metrics(self.bars, self.summary())
        last = self.bars[-1]["c"]
        self.assertEqual(m["last"], last)
        self.assertEqual(m["as_of"], self.bars[-1]["t"])
        self.assertAlmostEqual(m["day_chg_pct"], 0.2, places=2)
        self.assertEqual(m["first_trade_bar"], self.first)
        self.assertAlmostEqual(m["since_first_trade_pct"],
                               round((last / self.bars[-10]["c"] - 1) * 100, 2))
        self.assertAlmostEqual(m["since_latest_trade_pct"],
                               round((last / self.bars[-4]["c"] - 1) * 100, 2))
        expected_avg = (self.bars[-10]["c"] + self.bars[-4]["c"]) / 2
        self.assertAlmostEqual(m["avg_buy_price"], round(expected_avg, 4), places=4)
        self.assertEqual(m["vs_insider_avg_side"], "buy")
        self.assertIsNone(m["avg_sell_price"])
        self.assertEqual(m["vol_ratio_5d"], 3.0)          # 300k vs 100k
        self.assertEqual(m["net_vs_adv"], round(2_000_000 / ((100_000 * 16 + 300_000 * 4) / 20), 2))
        self.assertGreater(m["range_pos_pct"], 90)         # steady uptrend → near the high

    def test_verdict_only_for_net_buyers(self):
        if importlib.util.find_spec("yfinance") is None:
            self.skipTest("price_context needs yfinance")
        buy = im.compute_metrics(self.bars, self.summary("buy"))
        self.assertIsNotNone(buy["verdict"])
        self.assertIn(buy["verdict"]["emoji"], "🟢🟠🔴🟡🔵")
        sell = im.compute_metrics(self.bars, self.summary("sell", trades=[
            {"side": "sell", "shares": 100, "price": self.bars[-4]["c"], "trade_date": self.latest}]))
        self.assertIsNone(sell["verdict"])
        self.assertEqual(sell["vs_insider_avg_side"], "sell")

    def test_trade_after_last_bar_and_short_history(self):
        s = self.summary()
        s["first_trade_date"] = s["last_trade_date"] = "2999-01-01"
        m = im.compute_metrics(self.bars[-3:], s)
        self.assertIsNone(m["since_first_trade_pct"])
        self.assertIsNone(m["since_latest_trade_pct"])
        self.assertIsNone(m["chg_1m_pct"])
        self.assertIsNone(m["vol_ratio_5d"])


# ─── Price cache ───────────────────────────────────────────────────

class PriceCacheTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.fail = {}      # symbol → exception to raise
        self.clock = FakeClock(MARKET_EPOCH)

        def fetcher(symbol):
            self.calls.append(symbol)
            if symbol in self.fail:
                raise self.fail[symbol]
            return make_bars(n=30)

        self.cache = im.PriceCache(fetcher=fetcher, clock=self.clock, max_workers=2)

    def test_ttl_hit_and_expiry(self):
        v = self.cache.get_many(["1001", "1002"])
        self.assertEqual(sorted(self.calls), ["1001.KL", "1002.KL"])
        self.assertEqual(v["1001"]["status"], "ok")
        self.assertEqual(len(v["1001"]["bars"]), 30)

        self.clock.t += im.TTL_MARKET_SECS - 5      # still fresh → no requests
        self.cache.get_many(["1001", "1002"])
        self.assertEqual(len(self.calls), 2)

        self.clock.t += 10                          # expired → refetch
        self.cache.get_many(["1001", "1002"])
        self.assertEqual(len(self.calls), 4)

    def test_offhours_ttl_is_longer(self):
        self.assertEqual(self.cache.ok_ttl(MARKET_EPOCH), im.TTL_MARKET_SECS)
        self.assertEqual(self.cache.ok_ttl(WEEKEND_EPOCH), im.TTL_OFFHOURS_SECS)

    def test_failed_refresh_keeps_stale_bars(self):
        self.cache.get_many(["1001"])
        self.clock.t += im.TTL_MARKET_SECS + 1
        self.fail["1001.KL"] = ConnectionError("boom")
        v = self.cache.get_many(["1001"])["1001"]
        self.assertEqual(v["status"], "stale")
        self.assertEqual(len(v["bars"]), 30)
        self.assertIn("boom", v["error"])
        # negative cache: no retry until TTL_ERROR passes
        self.cache.get_many(["1001"])
        self.assertEqual(self.calls.count("1001.KL"), 2)
        self.clock.t += im.TTL_ERROR_SECS + 1
        self.cache.get_many(["1001"])
        self.assertEqual(self.calls.count("1001.KL"), 3)

    def test_rate_limit_opens_cooldown(self):
        class YFRateLimitError(Exception):
            pass

        self.fail["1001.KL"] = YFRateLimitError("Too Many Requests. Rate limited.")
        v = self.cache.get_many(["1001"])["1001"]
        self.assertEqual(v["error_kind"], "rate_limit")
        self.assertTrue(self.cache.in_cooldown())
        self.assertIsNotNone(self.cache.feed_status()["cooldown_until"])

        # New symbols are NOT fetched during cooldown
        v2 = self.cache.get_many(["2002"])["2002"]
        self.assertEqual(v2["status"], "cooldown")
        self.assertNotIn("2002.KL", self.calls)

        self.clock.t += im.COOLDOWN_SECS + 1
        self.cache.get_many(["2002"])
        self.assertIn("2002.KL", self.calls)

    def test_silent_ban_streak_opens_cooldown(self):
        for code in ("3001", "3002", "3003"):
            self.fail[im.yahoo_symbol(code)] = im.PriceFetchError("no_data", "no price data")
        self.cache.get_many(["3001", "3002", "3003"])
        self.assertTrue(self.cache.in_cooldown())

    def test_single_no_data_symbol_does_not_trip_cooldown(self):
        self.fail["03041.KL"] = im.PriceFetchError("no_data", "no price data on Yahoo for 03041.KL")
        v = self.cache.get_many(["03041", "1001"])
        self.assertEqual(v["03041"]["status"], "error")
        self.assertEqual(v["1001"]["status"], "ok")
        self.assertFalse(self.cache.in_cooldown())

    def test_missing_yfinance_is_a_setup_error(self):
        import sys
        from unittest import mock

        with mock.patch.dict(sys.modules, {"yfinance": None}):   # import → ImportError
            with self.assertRaises(im.PriceFetchError) as ctx:
                im.fetch_daily_bars("5352.KL")
        self.assertEqual(ctx.exception.kind, "setup")
        self.assertIn("pip install yfinance", str(ctx.exception))

        cache = im.PriceCache(fetcher=im.fetch_daily_bars, clock=self.clock)
        with mock.patch.dict(sys.modules, {"yfinance": None}):
            v = cache.get_many(["5352", "1155", "5347"])
        self.assertEqual({x["error_kind"] for x in v.values()}, {"setup"})
        self.assertFalse(cache.in_cooldown())    # not mistaken for a Yahoo ban

    def test_prune_forgets_symbols_nobody_asks_for(self):
        self.cache.get_many(["1001", "1002"])
        self.clock.t += im.ENTRY_MAX_AGE_SECS + 1
        self.cache.get_many(["1002"])          # 1001 not requested for > 2 days
        self.assertEqual(self.cache.feed_status()["cached_symbols"], 1)

    def test_classify_exception(self):
        class YFPricesMissingError(Exception):
            pass

        self.assertEqual(im._classify_exception(Exception("HTTP Error 429")), "rate_limit")
        self.assertEqual(im._classify_exception(YFPricesMissingError("x")), "no_data")
        self.assertEqual(im._classify_exception(Exception("$X.KL: possibly delisted")), "no_data")
        self.assertEqual(im._classify_exception(OSError("SSL connect failed")), "network")


# ─── Flask routes ──────────────────────────────────────────────────

class RouteTests(unittest.TestCase):
    def setUp(self):
        import app as app_module

        self.app_module = app_module
        self.db = DB()
        for i, code in enumerate(["4001", "4002", "4003", "4004", "4005", "4006", "4007"]):
            self.db.add(code, score=15 - i, shares=100_000 * (i + 1))
        self._orig_db, self._orig_cache = app_module.DB_PATH, im.PRICE_CACHE
        app_module.DB_PATH = self.db.path
        im.PRICE_CACHE = im.PriceCache(fetcher=lambda s: make_bars(n=60), clock=FakeClock(MARKET_EPOCH))
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.app_module.DB_PATH, im.PRICE_CACHE = self._orig_db, self._orig_cache
        self.db.close()

    def test_movers_page_and_nav(self):
        r = self.client.get("/movers")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"insider-movers", r.data)
        self.assertIn(b"function renderChart", r.data)   # raw block rendered verbatim
        for page in ("/insider",):
            self.assertIn(b'href="/movers"', self.client.get(page).data)

    def test_api_movers_top5_with_prices(self):
        data = self.client.get("/api/movers").get_json()
        self.assertEqual(data["params"], {"days": 7, "limit": 5, "rank": "score"})
        self.assertEqual(data["eligible"], 7)
        codes = [s["stock_code"] for s in data["stocks"]]
        self.assertEqual(codes, ["4001", "4002", "4003", "4004", "4005"])
        first = data["stocks"][0]
        self.assertEqual(first["price"]["status"], "ok")
        self.assertEqual(len(first["price"]["bars"]), 60)
        self.assertIsNotNone(first["metrics"]["last"])
        self.assertEqual(first["ticker"], "4001.KL")

    def test_api_movers_limit_is_capped(self):
        data = self.client.get("/api/movers?limit=50&rank=shares").get_json()
        self.assertEqual(data["params"]["limit"], 10)
        self.assertEqual(len(data["stocks"]), 7)
        self.assertEqual(data["stocks"][0]["stock_code"], "4007")


if __name__ == "__main__":
    unittest.main()
