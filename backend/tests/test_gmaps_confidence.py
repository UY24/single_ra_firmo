import unittest
from unittest import mock

from app.services.serpwow import legacy_app


def _result(*items):
    return {"results": list(items), "request_count": 1}


class TestGmapsScoring(unittest.TestCase):
    def test_select_best_unchanged_picks_name_match(self):
        gm = _result(
            {"title": "Other Place", "website": "https://other.com", "address": "1 X St"},
            {"title": "Acme Motors", "website": "https://acme-motors.com", "address": "5 Y Rd"},
        )
        self.assertEqual(
            legacy_app._select_best_gmaps_website(gm, "Acme Motors", None),
            "https://acme-motors.com",
        )

    def test_select_best_none_when_no_website(self):
        gm = _result({"title": "Acme", "address": "1 X St"})
        self.assertIsNone(legacy_app._select_best_gmaps_website(gm, "Acme Motors", None))

    def test_score_exposes_signals(self):
        gm = _result({"title": "Acme Motors", "website": "https://acme-motors.com",
                      "address": "500 Main Street"})
        scored = legacy_app._score_gmaps_candidates(gm, "Acme Motors", "500 Main Street")
        self.assertEqual(len(scored), 1)
        e = scored[0]
        self.assertEqual(e["url"], "https://acme-motors.com")
        self.assertTrue(e["name_match"])
        self.assertTrue(e["address_match"])


class TestGmapsConfidenceMapping(unittest.TestCase):
    def test_name_and_address_is_high(self):
        raw = legacy_app._gmaps_confidence_for_entry(
            {"url": "https://a.com", "name_match": True, "address_match": True,
             "address_conflict": False, "organizational_mismatch": False})
        self.assertEqual(raw["confidence_score"], 90)
        self.assertEqual(raw["confidence"], "high")
        self.assertEqual(raw["official_website"], "https://a.com")

    def test_name_only_is_medium(self):
        raw = legacy_app._gmaps_confidence_for_entry(
            {"url": "https://a.com", "name_match": True, "address_match": False,
             "address_conflict": False, "organizational_mismatch": False})
        self.assertEqual(raw["confidence_score"], 70)
        self.assertEqual(raw["confidence"], "medium")

    def test_neither_is_low_band(self):
        raw = legacy_app._gmaps_confidence_for_entry(
            {"url": "https://a.com", "name_match": False, "address_match": False,
             "address_conflict": False, "organizational_mismatch": False})
        self.assertEqual(raw["confidence_score"], 40)
        self.assertEqual(raw["confidence"], "low")

    def test_org_mismatch_penalty(self):
        raw = legacy_app._gmaps_confidence_for_entry(
            {"url": "https://a.com", "name_match": True, "address_match": True,
             "address_conflict": False, "organizational_mismatch": True})
        self.assertEqual(raw["confidence_score"], 70)  # 90 - 20
        self.assertTrue(raw["organizational_mismatch"])

    def test_address_conflict_penalty(self):
        raw = legacy_app._gmaps_confidence_for_entry(
            {"url": "https://a.com", "name_match": True, "address_match": False,
             "address_conflict": True, "organizational_mismatch": False})
        self.assertEqual(raw["confidence_score"], 55)  # 70 - 15

    def test_none_entry_is_zero(self):
        raw = legacy_app._gmaps_confidence_for_entry(None)
        self.assertEqual(raw["confidence_score"], 0)
        self.assertEqual(raw["confidence"], "low")
        self.assertIsNone(raw["official_website"])


class TestGmapsConfidenceBlock(unittest.TestCase):
    GM = {"results": [{"title": "Acme Motors", "website": "https://acme-motors.com",
                       "address": "500 Main Street"}], "request_count": 1}

    def test_heuristic_default(self):
        block = legacy_app._gmaps_confidence_block(
            self.GM, "Acme Motors", "500 Main Street", "https://acme-motors.com")
        self.assertEqual(block["mode"], "heuristic")
        self.assertEqual(block["raw"]["official_website"], "https://acme-motors.com")
        self.assertGreaterEqual(block["raw"]["confidence_score"], 60)

    def test_llm_mode_falls_back(self):
        with mock.patch.dict("os.environ", {"GMAPS_CONFIDENCE_MODE": "llm"}):
            block = legacy_app._gmaps_confidence_block(
                self.GM, "Acme Motors", "500 Main Street", "https://acme-motors.com")
        self.assertEqual(block["mode"], "heuristic (llm-fallback)")
        self.assertEqual(block["raw"]["official_website"], "https://acme-motors.com")

    def test_fallback_url_not_in_candidates_is_uncorroborated(self):
        # chosen_url came from extract_gmaps_website, not the scored set
        block = legacy_app._gmaps_confidence_block(
            self.GM, "Acme Motors", "500 Main Street", "https://elsewhere.example")
        self.assertEqual(block["raw"]["official_website"], "https://elsewhere.example")
        self.assertEqual(block["raw"]["confidence_score"], 40)

    def test_no_url_is_zero(self):
        block = legacy_app._gmaps_confidence_block(self.GM, "Acme Motors", None, None)
        self.assertEqual(block["raw"]["confidence_score"], 0)
        self.assertIsNone(block["raw"]["official_website"])
