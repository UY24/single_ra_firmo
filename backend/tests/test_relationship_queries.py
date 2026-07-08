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
    def test_all_four_phases_with_full_inputs(self):
        qs = build_relationship_phase_queries(
            "m25vc", "Sanzo", "New York", "United States", "m25vc.com")
        labels = [label for label, _ in qs]
        self.assertEqual(labels, ["phase1_relationship", "phase2_investment_evidence",
                                  "phase3_official_site", "phase4_portfolio_anchor"])
        by = dict(qs)
        self.assertIn('"m25vc"', by["phase1_relationship"])
        self.assertIn('"Sanzo"', by["phase1_relationship"])
        self.assertIn("official website", by["phase1_relationship"])
        self.assertIn("investment OR portfolio OR funding OR acquisition",
                      by["phase2_investment_evidence"])
        self.assertIn("New York", by["phase3_official_site"])
        self.assertIn("United States", by["phase3_official_site"])
        self.assertIn("site:m25vc.com", by["phase4_portfolio_anchor"])

    def test_no_x_yields_only_phase3(self):
        qs = build_relationship_phase_queries("", "Sanzo", "", "", "")
        self.assertEqual([label for label, _ in qs], ["phase3_official_site"])

    def test_no_input_url_drops_phase4(self):
        qs = build_relationship_phase_queries("m25vc", "Sanzo", "", "", "")
        labels = [label for label, _ in qs]
        self.assertNotIn("phase4_portfolio_anchor", labels)
        self.assertEqual(len(labels), 3)

    def test_max_phases_caps_output(self):
        qs = build_relationship_phase_queries(
            "m25vc", "Sanzo", "", "", "m25vc.com", max_phases=2)
        self.assertEqual(len(qs), 2)


if __name__ == "__main__":
    unittest.main()
