import unittest
from unittest import mock

from app.services.serpwow import engine as legacy_app


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

    def test_block_is_heuristic_only_regardless_of_env(self):
        # _gmaps_confidence_block no longer reads GMAPS_CONFIDENCE_MODE — the executor
        # owns the llm branch. The block is always the heuristic computation.
        with mock.patch.dict("os.environ", {"GMAPS_CONFIDENCE_MODE": "llm"}):
            block = legacy_app._gmaps_confidence_block(
                self.GM, "Acme Motors", "500 Main Street", "https://acme-motors.com")
        self.assertEqual(block["mode"], "heuristic")
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


class TestGmapsBandBoundaries(unittest.TestCase):
    """Pins the exact band string at the score boundaries documented on
    _gmaps_confidence_for_entry: high >= 80, medium 50-79, low < 50.

    Reachable scores are only those the signal combinations below can
    actually produce (base 90/70/60/40, with -15 address_conflict and/or
    -20 organizational_mismatch penalties, clamped to [0, 100]):
    {5, 20, 25, 35, 40, 45, 50, 55, 60, 70, 75, 90} (0 is separately
    reachable only via the no-url/None early-return path). We assert the
    band rule against each of those real, computed values rather than
    fabricating a score the function can't produce.
    """

    @staticmethod
    def _band_for(score: int) -> str:
        return "high" if score >= 80 else "medium" if score >= 50 else "low"

    def test_name_and_address_is_high_band(self):
        raw = legacy_app._gmaps_confidence_for_entry(
            {"url": "https://a.com", "name_match": True, "address_match": True,
             "address_conflict": False, "organizational_mismatch": False})
        self.assertEqual(raw["confidence_score"], 90)
        self.assertEqual(raw["confidence"], "high")
        self.assertEqual(raw["confidence"], self._band_for(raw["confidence_score"]))

    def test_name_only_is_medium_band(self):
        raw = legacy_app._gmaps_confidence_for_entry(
            {"url": "https://a.com", "name_match": True, "address_match": False,
             "address_conflict": False, "organizational_mismatch": False})
        self.assertEqual(raw["confidence_score"], 70)
        self.assertEqual(raw["confidence"], "medium")
        self.assertEqual(raw["confidence"], self._band_for(raw["confidence_score"]))

    def test_address_only_is_medium_band(self):
        raw = legacy_app._gmaps_confidence_for_entry(
            {"url": "https://a.com", "name_match": False, "address_match": True,
             "address_conflict": False, "organizational_mismatch": False})
        self.assertEqual(raw["confidence_score"], 60)
        self.assertEqual(raw["confidence"], "medium")
        self.assertEqual(raw["confidence"], self._band_for(raw["confidence_score"]))

    def test_neither_is_low_band(self):
        raw = legacy_app._gmaps_confidence_for_entry(
            {"url": "https://a.com", "name_match": False, "address_match": False,
             "address_conflict": False, "organizational_mismatch": False})
        self.assertEqual(raw["confidence_score"], 40)
        self.assertEqual(raw["confidence"], "low")
        self.assertEqual(raw["confidence"], self._band_for(raw["confidence_score"]))

    def test_name_and_address_with_org_mismatch_pins_70_boundary(self):
        # 90 - 20 = 70: still lands in medium, not high. Pins the top edge
        # of the medium band reachable via the org_mismatch penalty.
        raw = legacy_app._gmaps_confidence_for_entry(
            {"url": "https://a.com", "name_match": True, "address_match": True,
             "address_conflict": False, "organizational_mismatch": True})
        self.assertEqual(raw["confidence_score"], 70)
        self.assertEqual(raw["confidence"], "medium")
        self.assertEqual(raw["confidence"], self._band_for(raw["confidence_score"]))

    def test_name_only_with_address_conflict_pins_55(self):
        # 70 - 15 = 55: pins a mid-medium value near the boundary cluster.
        raw = legacy_app._gmaps_confidence_for_entry(
            {"url": "https://a.com", "name_match": True, "address_match": False,
             "address_conflict": True, "organizational_mismatch": False})
        self.assertEqual(raw["confidence_score"], 55)
        self.assertEqual(raw["confidence"], "medium")
        self.assertEqual(raw["confidence"], self._band_for(raw["confidence_score"]))

    def test_name_only_with_org_mismatch_pins_exact_50_boundary(self):
        # 70 - 20 = 50: the lowest score that is still "medium" (>= 50).
        raw = legacy_app._gmaps_confidence_for_entry(
            {"url": "https://a.com", "name_match": True, "address_match": False,
             "address_conflict": False, "organizational_mismatch": True})
        self.assertEqual(raw["confidence_score"], 50)
        self.assertEqual(raw["confidence"], "medium")
        self.assertEqual(raw["confidence"], self._band_for(raw["confidence_score"]))

    def test_name_only_with_both_penalties_pins_35_is_low(self):
        # 70 - 15 - 20 = 35: just below the medium band; closest reachable
        # score to the 49/50 edge on the low side (no combination of the
        # documented signals yields exactly 49).
        raw = legacy_app._gmaps_confidence_for_entry(
            {"url": "https://a.com", "name_match": True, "address_match": False,
             "address_conflict": True, "organizational_mismatch": True})
        self.assertEqual(raw["confidence_score"], 35)
        self.assertEqual(raw["confidence"], "low")
        self.assertEqual(raw["confidence"], self._band_for(raw["confidence_score"]))

    def test_band_rule_holds_across_all_reachable_scores(self):
        # Exhaustively derive every score reachable from the documented
        # signal combinations (4 base cases x address_conflict x
        # organizational_mismatch) and assert the band rule holds for each,
        # without asserting any fabricated/unreachable score.
        bases = {
            "name+address": (True, True, 90),
            "name-only": (True, False, 70),
            "address-only": (False, True, 60),
            "neither": (False, False, 40),
        }
        seen_scores = set()
        for name_match, address_match, base in bases.values():
            for address_conflict in (False, True):
                for organizational_mismatch in (False, True):
                    raw = legacy_app._gmaps_confidence_for_entry({
                        "url": "https://a.com",
                        "name_match": name_match,
                        "address_match": address_match,
                        "address_conflict": address_conflict,
                        "organizational_mismatch": organizational_mismatch,
                    })
                    seen_scores.add(raw["confidence_score"])
                    self.assertEqual(
                        raw["confidence"], self._band_for(raw["confidence_score"]),
                        msg=f"score={raw['confidence_score']} band={raw['confidence']}")
        # Sanity: this is the reachable-score set from these signal
        # combinations (0 is separately reachable only via the no-url/None
        # early-return path, not through this loop), confirming 50 is
        # reachable exactly and 49 is not.
        self.assertEqual(
            seen_scores, {5, 20, 25, 35, 40, 45, 50, 55, 60, 70, 75, 90})
        self.assertNotIn(49, seen_scores)
