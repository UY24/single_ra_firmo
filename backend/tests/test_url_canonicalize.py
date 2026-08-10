import unittest

from app.services.serpwow.engine import canonicalize_official_url, dedupe_candidate_urls
from app.services.serpwow.url_utils import url_matches_domain, x_domain_from_input_url


class TestCanonicalize(unittest.TestCase):
    def test_scheme_and_www_and_trailing_slash_equal(self):
        a = canonicalize_official_url("http://www.Example.com/")
        b = canonicalize_official_url("https://example.com")
        self.assertEqual(a, b)

    def test_preserves_meaningful_path(self):
        self.assertNotEqual(
            canonicalize_official_url("https://example.com/contact"),
            canonicalize_official_url("https://example.com/"),
        )

    def test_drops_fragment(self):
        self.assertEqual(
            canonicalize_official_url("https://example.com/p#top"),
            canonicalize_official_url("https://example.com/p"),
        )

    def test_empty_or_invalid(self):
        self.assertEqual(canonicalize_official_url(""), "")
        self.assertEqual(canonicalize_official_url("not a url"), "")

    def test_dedupe_by_canonical(self):
        out = dedupe_candidate_urls([
            "http://www.example.com/", "https://example.com",
            "https://example.com/contact", "https://other.com",
        ])
        self.assertEqual(out, ["http://www.example.com/", "https://example.com/contact", "https://other.com"])


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


if __name__ == "__main__":
    unittest.main()
