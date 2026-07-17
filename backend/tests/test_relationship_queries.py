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
        input_url = "https://www.m25vc.com/portfolio"
        qs = build_relationship_phase_queries("m25vc", "Sanzo", input_url)
        labels = [label for label, _ in qs]
        self.assertEqual(labels, [
            "phase1_relationship_and_url",
            "phase2_financial_event_evidence",
            "phase3_portfolio_identity",
        ])
        for _label, query in qs:
            self.assertIn('"m25vc"', query)
            self.assertIn('"Sanzo"', query)
            self.assertIn(input_url, query)
            self.assertIn("plain-text", query)
            self.assertIn("https://", query)
            self.assertIn("Do not return Company X", query)

        by = dict(qs)
        self.assertIn("documented financial relationship",
                      by["phase1_relationship_and_url"])
        for term in ("investment", "funding", "portfolio", "acquisition",
                     "ownership", "financial-backing"):
            self.assertIn(term, by["phase2_financial_event_evidence"])
        self.assertIn("extracted from a company logo",
                      by["phase3_portfolio_identity"])

    def test_missing_required_input_yields_no_queries(self):
        self.assertEqual(build_relationship_phase_queries("", "Sanzo", "https://x.test"), [])
        self.assertEqual(build_relationship_phase_queries("X", "", "https://x.test"), [])
        self.assertEqual(build_relationship_phase_queries("X", "Y", ""), [])


if __name__ == "__main__":
    unittest.main()
