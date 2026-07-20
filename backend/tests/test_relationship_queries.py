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
        qs = build_relationship_phase_queries("m25vc", "Sanzo", "m25vc.com")
        labels = [label for label, _ in qs]
        self.assertEqual(labels, [
            "phase1_official_website",
            "phase2_financial_evidence",
            "phase3_portfolio_anchor",
        ])
        for _label, query in qs:
            self.assertIn('"Sanzo"', query)
            # Keyword queries, not prose questions — a "?" collapses organic_results.
            self.assertNotIn("?", query)

        by = dict(qs)
        self.assertIn("official website", by["phase1_official_website"])
        self.assertIn('"Sanzo"', by["phase1_official_website"])
        for term in ("investment", "portfolio", "acquisition", " OR "):
            self.assertIn(term, by["phase2_financial_evidence"])
        self.assertIn('"m25vc"', by["phase2_financial_evidence"])
        self.assertIn("site:m25vc.com", by["phase3_portfolio_anchor"])

    def test_degraded_inputs(self):
        # No x_domain (Input_URL not a parseable host) → phase3 anchor dropped,
        # phase1 + phase2 still run.
        no_domain = build_relationship_phase_queries("m25vc", "Sanzo", "")
        self.assertEqual([l for l, _ in no_domain],
                         ["phase1_official_website", "phase2_financial_evidence"])
        # Both X and Y are required (X is what the gate verifies against).
        self.assertEqual(build_relationship_phase_queries("", "Sanzo", "m25vc.com"), [])
        self.assertEqual(build_relationship_phase_queries("m25vc", "", "m25vc.com"), [])


if __name__ == "__main__":
    unittest.main()
