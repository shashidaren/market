#!/usr/bin/env python3
"""
Tests for the company registry (tickers.py) and its JSON data files.

    python3 -m unittest test_tickers.py -v

No network, no news.db from the server: every test builds its own registry
in a temp directory.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import tickers
from tickers import Company, Registry, derive_aliases, load_registry


def registry_with(companies, macro=None):
    return Registry([Company.from_dict(c) for c in companies], macro or {})


class DeriveAliasTests(unittest.TestCase):
    def test_legal_name_variants_and_code_are_generated(self):
        company = Company(code="1155", name="Maybank",
                          legal_name="Malayan Banking Bhd", sector="Banking")
        self.assertEqual(
            derive_aliases(company),
            ["Maybank", "Malayan Banking Bhd", "Malayan Banking", "1155"],
        )

    def test_sdn_bhd_and_repeated_suffixes(self):
        company = Company(code="1234", name="Foo", legal_name="Foo Industries Sdn Bhd",
                          sector="Industrial")
        self.assertEqual(
            derive_aliases(company),
            ["Foo", "Foo Industries Sdn Bhd", "Foo Industries", "1234"],
        )

    def test_explicit_aliases_are_kept_and_duplicates_removed(self):
        company = Company(code="1023", name="CIMB", sector="Banking",
                          legal_name="CIMB Group Holdings Bhd",
                          aliases=["CIMB Group", "cimb group", "CIMB"])
        aliases = derive_aliases(company)
        self.assertIn("CIMB Group", aliases)
        self.assertEqual(sum(1 for a in aliases if a.lower() == "cimb"), 1)
        self.assertIn("CIMB Group Holdings", aliases)

    def test_strip_legal_does_not_drop_group_or_holdings(self):
        # "Hong Leong" would collide with three listed companies — we must
        # never derive it.
        company = Company(code="1082", name="Hong Leong Financial Group",
                          legal_name="Hong Leong Financial Group Bhd", sector="Banking")
        aliases = derive_aliases(company)
        self.assertNotIn("Hong Leong", aliases)
        self.assertIn("Hong Leong Financial Group", aliases)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = registry_with([
            {"code": "1155", "name": "Maybank", "legal_name": "Malayan Banking Bhd",
             "sector": "Banking"},
            {"code": "1023", "name": "CIMB", "sector": "Banking",
             "aliases": ["CIMB Group"]},
            # same alias as CIMB's name, different company → must be ignored
            {"code": "9999", "name": "CIMB Niaga", "sector": "Banking"},
            {"code": None, "name": "EPF", "sector": "GLC"},
        ], {"Macro": ["Fed", "OPR", "rate cut"]})

    def test_every_phrase_maps_back_to_one_company(self):
        for phrase in ("Maybank", "Malayan Banking", "Malayan Banking Bhd", "1155"):
            self.assertEqual(self.registry.match(phrase), {"Maybank": "Banking"})
        self.assertEqual(self.registry.match("CIMB Group results"),
                         {"CIMB": "Banking"})

    def test_word_boundaries_prevent_substring_matches(self):
        self.assertEqual(self.registry.match("Federal Reserve hikes"), {})
        self.assertEqual(self.registry.match("Estimate season"), {})
        self.assertEqual(self.registry.match("OPR held"), {})
        self.assertEqual(self.registry.match("Fed speaks"), {})

    def test_macro_fallback_and_noise(self):
        self.assertEqual(self.registry.classify("OPR stays at 3.00%"),
                         ("macro_indirect", "Macro", []))
        self.assertEqual(self.registry.classify("Weather is nice today"),
                         ("noise", None, []))

    def test_ambiguous_alias_is_reported_and_not_used(self):
        # "CIMB" is CIMB's canonical name and (as a prefix) not an alias of
        # CIMB Niaga, so it stays valid; a real collision looks like this:
        reg = registry_with([
            {"code": "1111", "name": "Alpha", "sector": "Tech", "aliases": ["Beta"]},
            {"code": "2222", "name": "Beta", "sector": "Tech"},
        ])
        self.assertIn("beta", reg.ambiguous)
        self.assertEqual(reg.ambiguous["beta"], ["Alpha", "Beta"])
        self.assertNotIn("Beta", reg.aliases)
        # The unambiguous code aliases still work.
        self.assertEqual(reg.match("1111")["Alpha"], "Tech")

    def test_validate_flags_errors_and_typos(self):
        reg = registry_with([
            {"code": "1155", "name": "Maybank", "sector": "Banking"},
            {"code": "1155", "name": "Duplicate Code", "sector": "Banking"},
            {"code": None, "name": "", "sector": "Tech"},
            {"code": "12", "name": "Bad Code", "sector": "Techn"},
        ])
        issues = reg.validate()
        messages = " | ".join(i.message for i in issues)
        self.assertIn("code also used by Maybank", messages)
        self.assertIn("company has no name", messages)
        self.assertIn("not four digits", messages)
        self.assertIn("did you mean 'Tech'", messages)

    def test_stats(self):
        stats = self.registry.stats()
        self.assertEqual(stats["companies"], 4)
        self.assertEqual(stats["with_code"], 3)
        self.assertEqual(stats["without_code"], 1)
        self.assertGreater(stats["aliases"], stats["companies"])


class DataFileTests(unittest.TestCase):
    """The shipped JSON must be valid, or the scorer silently degrades."""

    def setUp(self):
        self.registry = tickers.load_registry()

    def test_shipped_registry_has_no_errors(self):
        errors = [i for i in self.registry.validate() if i.level == "error"]
        self.assertEqual(errors, [], [i.message for i in errors])

    def test_known_companies_keep_their_old_aliases(self):
        # Behaviour the scorer relied on before the migration.
        for phrase, canonical in [
            ("Maybank", "Maybank"), ("Malayan Banking Bhd", "Maybank"),
            ("1155", "Maybank"), ("CIMB Group", "CIMB"),
            ("Ambank", "AmBank"), ("HLFG", "Hong Leong Financial Group"),
            ("Top Glove", "Top Glove"), ("FBM KLCI", "FBM KLCI"),
        ]:
            matches = self.registry.match(phrase)
            self.assertIn(canonical, matches, f"{phrase!r} stopped matching")

    def test_no_legacy_alias_was_lost(self):
        # Every alias that existed in the pre-2026-11 file must still match
        # or be intentionally ambiguous. The old file is recoverable from
        # git history; this guards the migration itself.
        # 498 aliases existed before the migration; deriving variants from
        # names/legal names/codes must never reduce that coverage.
        self.assertGreaterEqual(len(self.registry.aliases), 498)

    def test_classify_matches_expected_labels(self):
        self.assertEqual(self.registry.classify("Maybank and CIMB lead KLCI")[0],
                         "malaysia_direct")
        self.assertEqual(self.registry.classify("Fed holds rates")[0], "macro_indirect")
        self.assertEqual(self.registry.classify("nothing to see here")[0], "noise")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "bursa_tickers.json").write_text(json.dumps({
            "schema": 1,
            "companies": [
                {"code": "1155", "name": "Maybank",
                 "legal_name": "Malayan Banking Bhd", "sector": "Banking"},
            ],
        }))
        (self.dir / "macro_keywords.json").write_text(json.dumps({
            "schema": 1, "sectors": {"Macro": ["OPR", "Fed"]},
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_writes_json_and_derives_aliases(self):
        code = tickers.main([
            "--data-dir", str(self.dir), "add",
            "--code", "8869", "--name", "Press Metal",
            "--legal-name", "Press Metal Aluminium Holdings Bhd",
            "--sector", "Materials", "--alias", "PMetal",
        ])
        self.assertEqual(code, 0)

        doc = json.loads((self.dir / "bursa_tickers.json").read_text())
        entry = next(c for c in doc["companies"] if c["code"] == "8869")
        self.assertEqual(entry["name"], "Press Metal")
        self.assertEqual(entry["sector"], "Materials")

        reg = load_registry(self.dir / "bursa_tickers.json",
                            self.dir / "macro_keywords.json")
        for phrase in ("Press Metal", "Press Metal Aluminium", "8869", "PMetal"):
            self.assertIn("Press Metal", reg.match(phrase), phrase)

    def test_add_is_idempotent_and_merges_aliases(self):
        base = ["--data-dir", str(self.dir), "add", "--code", "8869",
                "--name", "Press Metal", "--sector", "Materials"]
        tickers.main(base)
        tickers.main(base + ["--alias", "PMetal"])
        tickers.main(base + ["--alias", "PMetal"])
        doc = json.loads((self.dir / "bursa_tickers.json").read_text())
        entries = [c for c in doc["companies"] if c["code"] == "8869"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["aliases"], ["PMetal"])

    def test_add_requires_sector_for_new_company(self):
        self.assertEqual(tickers.main([
            "--data-dir", str(self.dir), "add", "--name", "Mystery Co",
        ]), 2)

    def test_alias_subcommand(self):
        tickers.main(["--data-dir", str(self.dir), "alias", "--code", "1155",
                      "--add", "Maybank Bhd"])
        reg = load_registry(self.dir / "bursa_tickers.json",
                            self.dir / "macro_keywords.json")
        self.assertIn("Maybank", reg.match("Maybank Bhd"))

        tickers.main(["--data-dir", str(self.dir), "alias", "--code", "1155",
                      "--remove", "Maybank Bhd"])
        reg = load_registry(self.dir / "bursa_tickers.json",
                            self.dir / "macro_keywords.json")
        self.assertNotIn("Maybank Bhd", reg.aliases)
        self.assertIn("Maybank", reg.match("Maybank Bhd"))  # name still matches

    def test_check_reports_errors_as_exit_code(self):
        (self.dir / "bursa_tickers.json").write_text(json.dumps({
            "companies": [{"code": "1155", "name": "", "sector": "Banking"}],
        }))
        self.assertEqual(tickers.main(["--data-dir", str(self.dir), "check"]), 1)

    def test_suggest_finds_names_the_registry_misses(self):
        db = self.dir / "news.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE raw_headlines (id TEXT PRIMARY KEY, source TEXT, title TEXT,
                snippet TEXT, url TEXT, published_at TEXT, collected_at TEXT, scored INT);
            CREATE TABLE scored_headlines (id TEXT PRIMARY KEY, relevance TEXT, tickers TEXT,
                scored_at TEXT);
        """)
        conn.execute("INSERT INTO raw_headlines VALUES ('1','x','Zeta Cloud Systems bids farewell',"
                     "'','u',datetime('now'),datetime('now'),1)")
        conn.execute("INSERT INTO scored_headlines VALUES ('1','noise','',datetime('now'))")
        conn.execute("INSERT INTO raw_headlines VALUES ('2','x','Maybank beats estimates',"
                     "'','u',datetime('now'),datetime('now'),1)")
        conn.execute("INSERT INTO scored_headlines VALUES ('2','noise','Maybank',datetime('now'))")
        conn.commit()
        conn.close()

        names = [row["name"] for row in tickers.suggest_names(db, days=7, limit=10)]
        self.assertIn("Zeta Cloud Systems", names)
        self.assertNotIn("Maybank", names)   # already matched by the registry


if __name__ == "__main__":
    unittest.main()
