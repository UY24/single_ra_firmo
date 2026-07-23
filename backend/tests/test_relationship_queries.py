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
    def test_two_prose_phases_with_full_inputs(self):
        qs = build_relationship_phase_queries("M25 Ventures", "Sanzo", "m25vc.com")
        labels = [label for label, _ in qs]
        self.assertEqual(labels, [
            "phase1_relationship_and_url",
            "phase2_identity_and_relationship",
        ])
        for _label, query in qs:
            # Prose AI-Overview questions: mention Y, X (name + domain), ask for a
            # typed-out plain-text https URL (no hyperlink).
            self.assertIn('"Sanzo"', query)
            self.assertIn("M25 Ventures (m25vc.com)", query)
            self.assertIn("?", query)
            self.assertIn("https://", query)
            self.assertIn("plain-text", query)
            self.assertIn("hyperlink", query)

        by = dict(qs)
        self.assertIn("business or financial relationship", by["phase1_relationship_and_url"])
        self.assertIn('Who is "Sanzo"?', by["phase2_identity_and_relationship"])

    def test_x_identity_falls_back_to_name_without_domain(self):
        # No parseable domain → X is identified by name alone, both phases still run.
        qs = build_relationship_phase_queries("M25 Ventures", "Sanzo", "")
        self.assertEqual(len(qs), 2)
        for _label, query in qs:
            self.assertIn("M25 Ventures", query)
            self.assertNotIn("()", query)  # no empty "(domain)"

    def test_both_x_and_y_required(self):
        self.assertEqual(build_relationship_phase_queries("", "Sanzo", "m25vc.com"), [])
        self.assertEqual(build_relationship_phase_queries("m25vc", "", "m25vc.com"), [])


if __name__ == "__main__":
    unittest.main()
