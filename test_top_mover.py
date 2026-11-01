#!/usr/bin/env python3
"""
Tests for top_mover.py and the /top routes. No network: Yahoo is replaced
by a fake cache and every stock's bars are generated deterministically.

    python3 -m unittest test_top_mover.py -v
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

import insider_movers as im
import top_mover as tm

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
CREATE TABLE scored_headlines (
    id TEXT PRIMARY KEY, relevance TEXT, sector TEXT, tickers TEXT,
    sentiment REAL, impact_horizon TEXT, confidence REAL, cluster_key TEXT,
    rationale TEXT, scored_at TEXT);
"""


def _rng(seed):
    """Tiny deterministic LCG — no dependency on random's global state."""
    state = seed or 1

    def nxt():
        nonlocal state
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF
    return nxt


def make_bars(code="1155", n=260, vol=0.01, jumps=None, vol_spike=1.0,
              start=1.0):
    """Deterministic daily bars ending today; `jumps` maps day index → %."""
    jumps = jumps or {}
    rnd = _rng(int("".join(c for c in str(code) if c.isdigit()) or "1"))
    price = start
    bars = []
    for i in range(n):
        prev = price
        move = (rnd() - 0.5) * 2 * vol + jumps.get(i, 0.0)
        price = round(max(0.01, prev * (1 + move)), 4)
        high = round(max(prev, price) * (1 + vol / 2), 4)
        low = round(min(prev, price) * (1 - vol / 2), 4)
        volume = int(1_000_000 * (0.6 + rnd()))
        if i >= n - 5:
            volume = int(volume * vol_spike)
        day = (date.today() - timedelta(days=n - 1 - i)).isoformat()
        bars.append({"t": day, "o": round(prev, 4), "h": high, "l": low,
                     "c": price, "v": volume})
    return bars


class FakeCache:
    """Same surface as insider_movers.PriceCache, no threads, no Yahoo."""

    def __init__(self, bars_by_code=None, feed=None):
        self.bars_by_code = bars_by_code or {}
        self.feed = feed or {
            "source": "fake bars", "market_open": True, "ttl_secs": 600,
            "cooldown_until": None, "cached_symbols": len(self.bars_by_code),
            "requests_made": 0,
        }
        self.requested: list[str] = []

    def get_many(self, codes, wait_secs=None):
        self.requested.extend(codes)
        out = {}
        for code in codes:
            bars = self.bars_by_code.get(code)
            out[code] = {
                "symbol": f"{code}.KL",
                "status": "ok" if bars else "error",
                "error": None if bars else "no price data",
                "error_kind": None if bars else "no_data",
                "fetched_at": "2026-11-01T01:00:00+00:00",
                "age_secs": 60,
                "bars": bars,
            }
        return out

    def feed_status(self):
        return dict(self.feed)


class DB:
    def __init__(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self._ann = 1000

    def add_insider(self, code, *, score=8, shares=500_000, direction="ACQUISITION",
                    day="2026-10-28", person="TAN AH KOW", company=None):
        self._ann += 1
        ann = self._ann
        self.conn.execute(
            "INSERT INTO bursa_announcements VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
            (ann, code, company or f"COMPANY {code}", "insider filing", "insider",
             "DIRECTOR_S219", 1, day, "http://example.test"),
        )
        self.conn.execute(
            """INSERT INTO bursa_insider_details
               (ann_id, stock_code, company_name, published_date, person_name,
                transaction_type, shares_transacted, consideration, price_per_share,
                date_of_change, circumstances, signal_score)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ann, code, company or f"COMPANY {code}", day, person, direction,
             shares, "RM1.00", 1.0, day, "Acquisition via open market", score),
        )
        self.conn.commit()

    def add_headline(self, tickers, when="datetime('now')", relevance="malaysia_direct"):
        self.conn.execute(
            "INSERT INTO scored_headlines (id, relevance, tickers, scored_at) "
            f"VALUES (?, ?, ?, {when})",
            (f"h-{tickers}-{self.conn.total_changes}", relevance, tickers),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
        os.unlink(self.path)


# ─── Features ──────────────────────────────────────────────────────

class FeatureTests(unittest.TestCase):
    def test_base_rate_matches_hand_count(self):
        closes = [1.0]
        for i in range(1, 100):
            closes.append(round(closes[-1] * (1.05 if i % 10 == 0 else 1.001), 6))
        bars = [{"t": f"2026-01-{1 + i % 28:02d}", "o": c, "h": c, "l": c,
                 "c": c, "v": 1_000} for i, c in enumerate(closes)]
        features = tm.movement_features(bars, threshold_pct=3.0)
        changes = [abs(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
        hits = sum(1 for c in changes if c >= 3.0)
        expected = (hits + 1) / (len(changes) + 20)
        self.assertAlmostEqual(features["base_rate"], expected, places=6)
        self.assertEqual(features["base_rate_pct"], round(expected * 100, 1))

    def test_higher_threshold_lowers_base_rate(self):
        bars = make_bars(vol=0.02, jumps={100: 0.06, 150: -0.05, 200: 0.04})
        low = tm.movement_features(bars, threshold_pct=1.0)
        high = tm.movement_features(bars, threshold_pct=5.0)
        self.assertGreater(low["base_rate"], high["base_rate"])

    def test_volume_and_regime_features(self):
        bars = make_bars(vol=0.015, vol_spike=4.0)
        features = tm.movement_features(bars, threshold_pct=3.0)
        self.assertGreater(features["vol_ratio_5d"], 1.0)
        self.assertGreater(features["vol_last_ratio"], 1.0)
        self.assertIsNotNone(features["atr_regime"])
        self.assertIsNotNone(features["range_pos_pct"])
        self.assertEqual(features["bars"], len(bars))

    def test_short_history_degrades_gracefully(self):
        features = tm.movement_features(make_bars(n=1))
        self.assertIsNone(features["base_rate"])
        self.assertIsNone(features["chg_5d_pct"])
        self.assertIsNone(features["atr_regime"])


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.features = tm.movement_features(
            make_bars(vol=0.03, jumps={200: 0.06, 220: -0.05, 240: 0.04}),
            threshold_pct=3.0,
        )
        self.calm = dict(self.features, vol_ratio_5d=1.0, vol_last_ratio=1.0)
        self.hot = dict(self.features, vol_ratio_5d=4.0, vol_last_ratio=3.0)

    def test_volume_expansion_raises_probability(self):
        calm = tm.score_candidate(self.calm, {}, 3.0)
        hot = tm.score_candidate(self.hot, {}, 3.0)
        self.assertGreater(hot["probability"], calm["probability"])
        self.assertGreater(hot["multiplier"], calm["multiplier"])

    def test_probability_is_bounded_and_explained(self):
        result = tm.score_candidate(
            self.hot,
            {"news_mentions": 3, "news_days": 2,
             "insider": {"score": 12, "n_filings": 4, "direction": "buy",
                         "net_shares": 2_000_000, "net_vs_adv": 1.5}},
            3.0,
        )
        self.assertLessEqual(result["probability"], 0.95)
        self.assertGreaterEqual(result["probability"], 0.01)
        self.assertLessEqual(result["multiplier"], 4.0)
        self.assertEqual(result["confidence"], "high")
        texts = " | ".join(r["text"] for r in result["reasons"])
        self.assertIn("volume", texts)
        self.assertIn("headline", texts)
        self.assertIn("insider signal", texts)

    def test_no_history_uses_prior(self):
        features = tm.movement_features(make_bars(n=3), threshold_pct=3.0)
        result = tm.score_candidate(features, {}, 3.0)
        self.assertEqual(result["base_rate_source"], "prior (too little price history)")
        self.assertEqual(result["confidence"], "low")

    def test_expected_move_scales_with_conditions(self):
        calm = tm.score_candidate(self.calm, {}, 3.0)
        hot = tm.score_candidate(self.hot, {}, 3.0)
        self.assertGreater(hot["expected_abs_move_pct"], calm["expected_abs_move_pct"])


# ─── Universe / payload ────────────────────────────────────────────

class UniverseTests(unittest.TestCase):
    def setUp(self):
        self.db = DB()

    def tearDown(self):
        self.db.close()

    def test_merges_insider_and_news_candidates(self):
        self.db.add_insider("1155", score=9)
        self.db.add_headline("Maybank,Petronas Chemicals,EPF")
        self.db.add_headline("Maybank")

        config = tm.parse_params({"shortlist": 12})
        universe = tm.build_universe(self.db.conn, config)

        codes = {c["stock_code"] for c in universe["shortlist"]}
        self.assertEqual(codes, {"1155", "5183"})   # EPF has no Bursa code
        maybank = next(c for c in universe["shortlist"] if c["stock_code"] == "1155")
        self.assertEqual(maybank["news_mentions"], 2)
        self.assertEqual(maybank["insider"]["score"], 9)
        self.assertEqual(maybank["sector"], "Banking")
        self.assertEqual(universe["counts"]["news_only"], 1)
        self.assertEqual(universe["counts"]["insider"], 1)

    def test_shortlist_trims_and_ranks_by_interest(self):
        for i, code in enumerate(["1155", "1023", "5183", "5347", "7113"]):
            self.db.add_insider(code, score=10 - i, company=f"COMPANY {code}")
        universe = tm.build_universe(self.db.conn, tm.parse_params({"shortlist": 3}))
        self.assertEqual(universe["counts"]["total"], 5)
        self.assertEqual([c["stock_code"] for c in universe["shortlist"]],
                         ["1155", "1023", "5183"])

    def test_missing_tables_are_reported_not_fatal(self):
        conn = sqlite3.connect(":memory:")
        universe = tm.build_universe(conn, tm.parse_params({}))
        self.assertEqual(universe["shortlist"], [])
        self.assertTrue(universe["insider_error"])
        self.assertTrue(universe["news_error"])
        conn.close()


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.db = DB()
        self.db.add_insider("1155", score=9)     # high insider score, calm price
        self.db.add_insider("1023", score=5)     # lower score, big volume spike
        self.db.add_headline("CIMB")

        self.calm = make_bars("1155", vol=0.004)
        self.volatile = make_bars("1023", vol=0.03,
                                  jumps={200: 0.06, 220: -0.05, 240: 0.04},
                                  vol_spike=6.0)
        self.config = tm.parse_params({"shortlist": 12, "runners": 4})

    def tearDown(self):
        self.db.close()

    def test_pick_is_the_highest_probability(self):
        cache = FakeCache({"1155": self.calm, "1023": self.volatile})
        payload = tm.build_payload(self.db.conn, self.config, cache=cache)

        self.assertIsNotNone(payload["pick"])
        pick = payload["pick"]
        self.assertEqual(pick["stock_code"], "1023")
        self.assertEqual(pick["sector"], "Banking")
        self.assertIn("bars", pick)
        self.assertEqual(len(pick["bars"]), len(self.volatile))
        self.assertEqual(pick["symbol"], "1023.KL")
        self.assertGreater(pick["probability"], 0.0)
        self.assertEqual(payload["ranked"][0]["stock_code"], "1023")
        for runner in payload["runners"]:
            self.assertNotIn("bars", runner)
        self.assertIn("disclaimer", payload)
        self.assertEqual(payload["threshold_pct"], tm.MOVE_THRESHOLD_PCT)

    def test_no_price_data_is_reported_not_ranked(self):
        cache = FakeCache({"1023": self.volatile})   # 1155 missing
        payload = tm.build_payload(self.db.conn, self.config, cache=cache)
        self.assertEqual(payload["pick"]["stock_code"], "1023")
        self.assertEqual([c["stock_code"] for c in payload["no_price"]], ["1155"])
        self.assertEqual(payload["no_price"][0]["status"], "error")

    def test_prices_can_be_skipped(self):
        payload = tm.build_payload(self.db.conn, self.config, with_prices=False)
        self.assertIsNone(payload["pick"])
        self.assertEqual(len(payload["no_price"]), 2)

    def test_runner_count_is_honoured(self):
        extra = FakeCache({
            "1155": self.calm,
            "1023": self.volatile,
        })
        payload = tm.build_payload(self.db.conn, self.config, cache=extra)
        self.assertEqual(len(payload["runners"]), min(1, self.config["limit"]))

    def test_parse_params_clamps(self):
        config = tm.parse_params({"shortlist": 999, "days": 999, "news_days": 0,
                                  "runners": 99})
        self.assertEqual(config["shortlist"], tm.MAX_SHORTLIST)
        self.assertEqual(config["insider_days"], tm.MAX_INSIDER_DAYS)
        self.assertEqual(config["news_days"], 1)
        self.assertEqual(config["limit"], 10)


# ─── Flask routes ──────────────────────────────────────────────────

class RouteTests(unittest.TestCase):
    def setUp(self):
        import app as app_module

        self.app_module = app_module
        self.db = DB()
        self.db.add_insider("1155", score=9)
        self.db.add_insider("1023", score=5)

        self._orig_db = app_module.DB_PATH
        self._orig_cache = im.PRICE_CACHE
        app_module.DB_PATH = self.db.path
        im.PRICE_CACHE = FakeCache({
            "1155": make_bars("1155", vol=0.004),
            "1023": make_bars("1023", vol=0.03, vol_spike=5.0),
        })
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.app_module.DB_PATH = self._orig_db
        im.PRICE_CACHE = self._orig_cache
        self.db.close()

    def test_top_page_and_nav(self):
        response = self.client.get("/top")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"top-mover", response.data)
        self.assertIn(b"function drawChart", response.data)   # raw block is verbatim
        for page in ("/", "/insider", "/movers"):
            self.assertIn(b'href="/top"', self.client.get(page).data)

    def test_api_top_mover(self):
        data = self.client.get("/api/top-mover").get_json()
        self.assertIn("pick", data)
        self.assertIn("runners", data)
        self.assertIn("universe", data)
        self.assertEqual(data["params"]["insider_days"], 10)
        self.assertIsNotNone(data["pick"])
        self.assertIn(data["pick"]["stock_code"], {"1155", "1023"})

    def test_api_shortlist_and_runners_are_capped(self):
        data = self.client.get("/api/top-mover?shortlist=999&runners=99").get_json()
        self.assertEqual(data["params"]["shortlist"], tm.MAX_SHORTLIST)
        self.assertLessEqual(len(data["runners"]), 10)


if __name__ == "__main__":
    unittest.main()
