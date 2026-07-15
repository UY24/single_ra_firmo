import unittest

from app.services.serpwow.engine import canonicalize_official_url, dedupe_candidate_urls
from app.services.serpwow.url_utils import is_disallowed_official_url


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

    def test_filename_shaped_hosts_are_not_globally_blocked(self):
        for url in ("https://main.py", "https://report.md", "https://setup.sh"):
            with self.subTest(url=url):
                self.assertFalse(is_disallowed_official_url(url))


if __name__ == "__main__":
    unittest.main()
