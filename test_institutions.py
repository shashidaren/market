#!/usr/bin/env python3
"""
Tests for institutions.py and the /institutions routes. No network needed —
a synthetic SQLite DB in a temp file supplies the S138 filings.

    python3 -m unittest test_institutions.py -v
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import institutions as inst

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


def utc_day(offset: int) -> str:
    """ISO date `offset` days before today (UTC, like SQLite's date('now'))."""
    return (datetime.now(timezone.utc).date() - timedelta(days=offset)).isoformat()


class DB:
    """Tiny helper to build an announcements DB in a temp file."""

    def __init__(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.next_id = 2000

    def add(self, *, code="1155", company="MALAYAN BANKING BHD",
            person="EMPLOYEES PROVIDENT FUND BOARD", tx="ACQUISITION",
            shares=1_000_000, pub=2, trade=4, price=None, consideration=None,
            pct=6.5, subcat="SUBSTANTIAL_S138"):
        self.next_id += 1
        pub_d = utc_day(pub)
        trade_d = utc_day(trade)
        self.conn.execute(
            "INSERT INTO bursa_announcements VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.next_id, code, company,
             "Changes in Sub. S-hldr's Int (Section 138)", "INSIDER_TRADE",
             subcat, 1, pub_d, f"https://example/{self.next_id}", "now"))
        self.conn.execute(
            """INSERT INTO bursa_insider_details
               (ann_id, stock_code, company_name, published_date, person_name,
                transaction_type, shares_transacted, consideration, price_per_share,
                direct_pct_after, date_of_change, parsed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.next_id, code, company, pub_d, person, tx, shares,
             consideration, price, pct, trade_d, "now"))
        self.conn.commit()
        return self.next_id

    def close(self):
        self.conn.close()
        os.unlink(self.path)


# ─── Fund matching ─────────────────────────────────────────────────

class FundMatchTests(unittest.TestCase):
    def test_epf_variants(self):
        for name in ("EMPLOYEES PROVIDENT FUND BOARD",
                     "Employees Provident Fund Board",
                     "EPF",
                     "KWSP",
                     "Kumpulan Wang Simpanan Pekerja"):
            fund = inst.classify_fund(name)
            self.assertIsNotNone(fund, name)
            self.assertEqual(fund.key, "EPF", name)

    def test_kwap_variants(self):
        for name in ("KWAP", "Kumpulan Wang Persaraan (Diperbadankan)",
                     "KUMPULAN WANG PERSARAAN", "Retirement Fund (Incorporated)"):
            self.assertEqual(inst.classify_fund(name).key, "KWAP", name)

    def test_asb_files_through_asnb(self):
        for name in ("AMANAH SAHAM BUMIPUTERA",
                     "Amanah Saham Nasional Bhd",
                     "ASNB",
                     "ASB 2"):
            self.assertEqual(inst.classify_fund(name).key, "ASB", name)

    def test_optional_funds(self):
        self.assertEqual(inst.classify_fund("PERMODALAN NASIONAL BERHAD").key, "PNB")
        self.assertEqual(inst.classify_fund("KHAZANAH NASIONAL BERHAD").key, "KHAZANAH")
        self.assertEqual(inst.classify_fund("LEMBAGA TABUNG HAJI").key, "TABUNG_HAJI")
        self.assertEqual(inst.classify_fund("URUSHARTA JAMAAH SDN BHD").key, "URUSHARTA")

    def test_short_codes_are_anchored(self):
        # must NOT be mistaken for a fund
        self.assertIsNone(inst.classify_fund("ASB RESOURCES SDN BHD"))
        self.assertIsNone(inst.classify_fund("ASBESTOS HOLDINGS BHD"))
        self.assertIsNone(inst.classify_fund("KWAPP NOMINEES SDN BHD"))

    def test_individuals_and_unknowns_are_none(self):
        self.assertIsNone(inst.classify_fund("TAN AH KOW"))
        self.assertIsNone(inst.classify_fund("PUBLIC MUTUAL BERHAD"))
        self.assertIsNone(inst.classify_fund(None))
        self.assertIsNone(inst.classify_fund("   "))

    def test_catalog_shape(self):
        catalog = inst.fund_catalog()
        keys = {f["key"] for f in catalog}
        self.assertTrue({"EPF", "KWAP", "ASB"} <= keys)
        tracked = {f["key"] for f in catalog if f["tracked"]}
        self.assertEqual(tracked, {"EPF", "KWAP", "ASB"})
        for entry in catalog:
            self.assertFalse(set(entry) & {"patterns"})   # no regexes leak to the UI


# ─── Prices / size sanity ──────────────────────────────────────────

class PriceTests(unittest.TestCase):
    def test_clean_price_passes_through(self):
        self.assertEqual(inst.per_share_price("RM0.870", 0.87), 0.87)

    def test_total_consideration_is_rejected(self):
        # parse_detail's clean_float turns "RM1,250,000" into 1250000.0
        self.assertIsNone(inst.per_share_price("RM1,250,000", 1_250_000.0))

    def test_range_gives_midpoint(self):
        self.assertAlmostEqual(inst.per_share_price("RM0.85 - RM0.90", None), 0.875)

    def test_text_mentioning_a_price(self):
        self.assertEqual(inst.per_share_price("RM0.845 per share", None), 0.845)

    def test_nothing_parseable(self):
        self.assertIsNone(inst.per_share_price(None, None))
        self.assertIsNone(inst.per_share_price("market price", None))


# ─── Params ────────────────────────────────────────────────────────

class ParamTests(unittest.TestCase):
    def test_defaults_to_tracked_funds(self):
        config = inst.parse_params({})
        self.assertEqual(config["funds"], ["EPF", "KWAP", "ASB"])
        self.assertEqual(config["side"], "all")
        self.assertEqual(config["days"], inst.DEFAULT_DAYS)

    def test_fund_list_and_all(self):
        self.assertEqual(inst.parse_params({"fund": "kwap, epf"}).get("funds"),
                         ["KWAP", "EPF"])
        self.assertEqual(len(inst.parse_params({"fund": "all"})["funds"]),
                         len(inst.FUNDS))

    def test_unknown_fund_falls_back_to_defaults(self):
        self.assertEqual(inst.parse_params({"fund": "NOPE"}).get("funds"),
                         ["EPF", "KWAP", "ASB"])

    def test_clamps(self):
        config = inst.parse_params({"days": 99999, "limit": 99999, "side": "sideways"})
        self.assertEqual(config["days"], inst.MAX_DAYS)
        self.assertEqual(config["limit"], inst.MAX_LIMIT)
        self.assertEqual(config["side"], "all")
        config = inst.parse_params({"days": -5, "limit": 0})
        self.assertEqual(config["days"], inst.MIN_DAYS)
        self.assertEqual(config["limit"], 1)


# ─── Payload ───────────────────────────────────────────────────────

class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.db = DB()

    def tearDown(self):
        self.db.close()

    def test_recent_is_newest_first_and_classified(self):
        self.db.add(person="EMPLOYEES PROVIDENT FUND BOARD", trade=20, pub=18)
        self.db.add(person="KWAP", trade=3, pub=1, tx="DISPOSAL", code="1023")
        self.db.add(person="AMANAH SAHAM NASIONAL BHD", trade=9, pub=7, code="5347")

        payload = inst.build_payload(self.db.conn, inst.parse_params({"days": 90}))
        self.assertIsNone(payload["error"])
        feed = payload["recent"]
        self.assertEqual([e["fund"] for e in feed], ["KWAP", "ASB", "EPF"])
        self.assertEqual(feed[0]["side"], "sell")
        self.assertEqual(feed[0]["stock_code"], "1023")
        self.assertEqual(feed[1]["side"], "buy")
        self.assertEqual(payload["counts"]["filings"], 3)

    def test_non_fund_filers_are_excluded_and_reported(self):
        self.db.add(person="EMPLOYEES PROVIDENT FUND BOARD")
        self.db.add(person="TAN AH KOW", shares=5_000_000)
        payload = inst.build_payload(self.db.conn, inst.parse_params({}))
        self.assertEqual(payload["counts"]["filings"], 1)
        self.assertEqual(payload["unmatched"][0]["name"], "TAN AH KOW")
        self.assertEqual(payload["counts"]["unmatched_names"], 1)

    def test_director_filings_are_ignored(self):
        # an S219 row must never show as an institution, even with a fund name
        self.db.add(person="EMPLOYEES PROVIDENT FUND BOARD", subcat="DIRECTOR_S219")
        payload = inst.build_payload(self.db.conn, inst.parse_params({}))
        self.assertEqual(payload["counts"]["filings"], 0)
        self.assertEqual(payload["recent"], [])
        self.assertTrue(all(f["n_filings"] == 0 for f in payload["summary"]))

    def test_duplicate_filings_are_dropped_once(self):
        for _ in range(3):   # same fund / stock / date / side / shares
            self.db.add(person="EMPLOYEES PROVIDENT FUND BOARD", trade=6, pub=5)
        payload = inst.build_payload(self.db.conn, inst.parse_params({}))
        self.assertEqual(payload["counts"]["filings"], 1)
        self.assertEqual(payload["counts"]["duplicates_dropped"], 2)

    def test_same_trade_by_two_funds_is_kept(self):
        self.db.add(person="EMPLOYEES PROVIDENT FUND BOARD", trade=6, pub=5)
        self.db.add(person="KWAP", trade=6, pub=5)
        payload = inst.build_payload(self.db.conn, inst.parse_params({}))
        self.assertEqual(payload["counts"]["filings"], 2)

    def test_summary_flows_and_top_buy_sell(self):
        self.db.add(person="EMPLOYEES PROVIDENT FUND BOARD", code="1155",
                    tx="ACQUISITION", shares=2_000_000)
        self.db.add(person="EMPLOYEES PROVIDENT FUND BOARD", code="1023",
                    tx="DISPOSAL", shares=500_000)
        payload = inst.build_payload(self.db.conn, inst.parse_params({}))
        epf = next(f for f in payload["summary"] if f["key"] == "EPF")
        self.assertEqual(epf["bought"], 2_000_000)
        self.assertEqual(epf["sold"], 500_000)
        self.assertEqual(epf["net_shares"], 1_500_000)
        self.assertEqual(epf["direction"], "buy")
        self.assertEqual(epf["top_buy"]["stock_code"], "1155")
        self.assertEqual(epf["top_sell"]["stock_code"], "1023")
        self.assertEqual(epf["n_stocks"], 2)
        self.assertEqual(epf["latest"][0]["stock_code"], "1023")  # newest first

    def test_values_only_when_price_is_plausible(self):
        self.db.add(person="KWAP", shares=1_000_000, price=0.5)
        self.db.add(person="KWAP", shares=1_000_000, consideration="RM1,000,000",
                    price=1_000_000.0, trade=8, pub=7)
        payload = inst.build_payload(self.db.conn, inst.parse_params({"fund": "KWAP"}))
        kwap = payload["summary"][0]
        self.assertEqual(kwap["priced_filings"], 1)
        self.assertAlmostEqual(kwap["bought_value_rm"], 500_000.0)
        values = [e["value_rm"] for e in payload["recent"]]
        self.assertIn(None, values)          # the total-value filing
        self.assertIn(500_000.0, values)

    def test_side_filter(self):
        self.db.add(person="KWAP", tx="ACQUISITION")
        self.db.add(person="KWAP", tx="DISPOSAL", code="1023")
        payload = inst.build_payload(self.db.conn, inst.parse_params({"side": "sell"}))
        self.assertEqual([e["side"] for e in payload["recent"]], ["sell"])
        self.assertEqual(payload["counts"]["feed_filings"], 1)

    def test_fund_filter_scopes_every_board(self):
        self.db.add(person="EMPLOYEES PROVIDENT FUND BOARD", code="1155")
        self.db.add(person="KWAP", code="1023")
        payload = inst.build_payload(self.db.conn, inst.parse_params({"fund": "EPF"}))
        self.assertEqual([f["key"] for f in payload["summary"]], ["EPF"])
        self.assertEqual([e["stock_code"] for e in payload["recent"]], ["1155"])
        self.assertEqual([s["stock_code"] for s in payload["stocks"]], ["1155"])

    def test_stock_board_combines_funds(self):
        self.db.add(person="EMPLOYEES PROVIDENT FUND BOARD", code="1155",
                    tx="ACQUISITION", shares=1_000_000)
        self.db.add(person="KWAP", code="1155", tx="DISPOSAL", shares=400_000)
        payload = inst.build_payload(self.db.conn, inst.parse_params({}))
        stock = payload["stocks"][0]
        self.assertEqual(stock["stock_code"], "1155")
        self.assertEqual(stock["bought"], 1_000_000)
        self.assertEqual(stock["sold"], 400_000)
        self.assertEqual(stock["net_shares"], 600_000)
        self.assertEqual({f["key"] for f in stock["funds"]}, {"EPF", "KWAP"})

    def test_missing_tables_reported_not_raised(self):
        conn = sqlite3.connect(":memory:")
        payload = inst.build_payload(conn, inst.parse_params({}))
        conn.close()
        self.assertIn("insider tables not available", payload["error"])
        self.assertEqual(payload["recent"], [])

    def test_window_excludes_old_filings(self):
        self.db.add(person="KWAP", pub=100, trade=101)
        payload = inst.build_payload(self.db.conn, inst.parse_params({"days": 30}))
        self.assertEqual(payload["counts"]["filings"], 0)

    def test_rows_without_a_side_are_skipped(self):
        self.db.add(person="KWAP", tx=None)
        payload = inst.build_payload(self.db.conn, inst.parse_params({}))
        self.assertEqual(payload["counts"]["filings"], 0)


# ─── Flask routes ──────────────────────────────────────────────────

class RouteTests(unittest.TestCase):
    def setUp(self):
        import app as app_module

        self.app_module = app_module
        self.db = DB()
        self.db.add(person="EMPLOYEES PROVIDENT FUND BOARD", code="1155")
        self.db.add(person="KWAP", code="1023", tx="DISPOSAL")

        self._orig_db = app_module.DB_PATH
        app_module.DB_PATH = self.db.path
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.app_module.DB_PATH = self._orig_db
        self.db.close()

    def test_page_and_nav(self):
        response = self.client.get("/institutions")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"institutional-flows", response.data)
        self.assertIn(b"most recent filings", response.data)
        for page in ("/", "/insider", "/movers", "/top"):
            self.assertIn(b'href="/institutions"', self.client.get(page).data)

    def test_api_payload(self):
        data = self.client.get("/api/institutions").get_json()
        self.assertIsNone(data["error"])
        self.assertEqual(data["counts"]["filings"], 2)
        self.assertEqual([f["key"] for f in data["summary"]], ["EPF", "KWAP", "ASB"])
        self.assertIn("disclaimer", data)
        self.assertTrue(any(u["key"] == "EPF" for u in data["funds"]))

    def test_api_params_are_clamped(self):
        data = self.client.get("/api/institutions?days=99999&limit=99999").get_json()
        self.assertEqual(data["params"]["days"], inst.MAX_DAYS)
        self.assertEqual(data["params"]["limit"], inst.MAX_LIMIT)

    def test_api_fund_filter(self):
        data = self.client.get("/api/institutions?fund=KWAP&side=sell").get_json()
        self.assertEqual([e["fund"] for e in data["recent"]], ["KWAP"])
        self.assertEqual(data["params"]["funds"], ["KWAP"])

    def test_api_funds_catalog(self):
        data = self.client.get("/api/institutions/funds").get_json()
        self.assertEqual({f["key"] for f in data} >= {"EPF", "KWAP", "ASB"}, True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
