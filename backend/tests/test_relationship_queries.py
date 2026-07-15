import unittest

from app.services.serpwow import query_builders
from app.services.serpwow.url_utils import url_matches_domain, x_domain_from_input_url


class TestXDomainHelpers(unittest.TestCase):
    def test_x_domain_from_input_url(self):
        self.assertEqual(
            x_domain_from_input_url("https://www.m25vc.com/portfolio"), "m25vc.com")
        self.assertEqual(x_domain_from_input_url("http://eastlinkcap.com"), "eastlinkcap.com")
        self.assertEqual(
            x_domain_from_input_url("eastlinkcap.com/portfolio"), "eastlinkcap.com")
        self.assertEqual(x_domain_from_input_url("www.m25vc.com/p"), "m25vc.com")
        self.assertEqual(x_domain_from_input_url(""), "")
        self.assertEqual(x_domain_from_input_url("not a url"), "")
        self.assertEqual(x_domain_from_input_url("report.pdf"), "")

    def test_url_matches_domain(self):
        self.assertTrue(url_matches_domain("https://m25vc.com/about", "m25vc.com"))
        self.assertTrue(url_matches_domain("https://www.m25vc.com/x", "m25vc.com"))
        self.assertTrue(url_matches_domain("https://blog.m25vc.com/", "m25vc.com"))
        self.assertFalse(url_matches_domain("https://notm25vc.com/", "m25vc.com"))
        self.assertFalse(url_matches_domain("https://modal.com/", "m25vc.com"))
        self.assertFalse(url_matches_domain("https://modal.com/", ""))


class TestRelationshipQueryMigration(unittest.TestCase):
    def test_obsolete_parallel_relationship_builder_is_removed(self):
        self.assertFalse(hasattr(query_builders, "build_relationship_phase_queries"))


if __name__ == "__main__":
    unittest.main()
