import unittest

from app.services.serpwow.query_builders import build_relationship_phase_queries
from app.services.serpwow.url_utils import url_matches_domain, x_domain_from_input_url


class TestXDomainHelpers(unittest.TestCase):
    def test_x_domain_from_input_url(self):
        self.assertEqual(
            x_domain_from_input_url("https://www.m25vc.com/portfolio"), "m25vc.com")
        self.assertEqual(x_domain_from_input_url("http://eastlinkcap.com"), "eastlinkcap.com")
        self.assertEqual(x_domain_from_input_url(""), "")
        self.assertEqual(x_domain_from_input_url("not a url"), "")

    def test_url_matches_domain(self):
        self.assertTrue(url_matches_domain("https://m25vc.com/about", "m25vc.com"))
        self.assertTrue(url_matches_domain("https://www.m25vc.com/x", "m25vc.com"))
        self.assertTrue(url_matches_domain("https://blog.m25vc.com/", "m25vc.com"))
        self.assertFalse(url_matches_domain("https://notm25vc.com/", "m25vc.com"))
        self.assertFalse(url_matches_domain("https://modal.com/", "m25vc.com"))
        self.assertFalse(url_matches_domain("https://modal.com/", ""))


class TestBuildRelationshipPhaseQueries(unittest.TestCase):
    def test_three_phases_with_full_inputs(self):
        # phase3 (Y-only "official website") was removed; every remaining phase must
        # anchor to X by name and/or domain.
        qs = build_relationship_phase_queries(
            "m25vc", "Sanzo", "New York", "United States", "m25vc.com")
        labels = [label for label, _ in qs]
        self.assertEqual(labels, ["phase1_relationship", "phase2_investment_evidence",
                                  "phase4_portfolio_anchor"])
        self.assertNotIn("phase3_official_site", labels)
        by = dict(qs)
        # phase1: X name + X domain + Y + relationship intent
        self.assertIn('"m25vc"', by["phase1_relationship"])
        self.assertIn("m25vc.com", by["phase1_relationship"])
        self.assertIn('"Sanzo"', by["phase1_relationship"])
        self.assertIn("financial relationship", by["phase1_relationship"])
        # phase2: X name + X domain as keywords (no site: restriction) + Y
        self.assertIn("m25vc.com", by["phase2_investment_evidence"])
        self.assertNotIn("site:", by["phase2_investment_evidence"])
        self.assertIn("investment OR portfolio OR funding OR acquisition",
                      by["phase2_investment_evidence"])
        # phase4: X name + Y restricted to X's own domain
        self.assertIn('"m25vc"', by["phase4_portfolio_anchor"])
        self.assertIn("site:m25vc.com", by["phase4_portfolio_anchor"])

    def test_no_x_yields_no_queries(self):
        # With no X there is nothing to anchor to; those rows short-circuit at the
        # LLM gate anyway, so no searches should fire.
        qs = build_relationship_phase_queries("", "Sanzo", "", "", "")
        self.assertEqual(qs, [])

    def test_no_input_url_drops_phase4_and_domain_text(self):
        # X but no domain: only phase1/phase2, and neither mentions a domain.
        qs = build_relationship_phase_queries("m25vc", "Sanzo", "", "", "")
        labels = [label for label, _ in qs]
        self.assertEqual(labels, ["phase1_relationship", "phase2_investment_evidence"])
        by = dict(qs)
        self.assertNotIn("website:", by["phase1_relationship"])
        self.assertNotIn("m25vc.com", by["phase2_investment_evidence"])

    def test_max_phases_caps_output(self):
        qs = build_relationship_phase_queries(
            "m25vc", "Sanzo", "", "", "m25vc.com", max_phases=2)
        self.assertEqual(len(qs), 2)


if __name__ == "__main__":
    unittest.main()
