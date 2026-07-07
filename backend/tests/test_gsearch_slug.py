import unittest

from app.services.serpwow import engine as legacy_app


class TestCompanySlug(unittest.TestCase):
    def test_slug_matches_ai_mode_style(self):
        self.assertEqual(legacy_app._company_slug("ISI Market Test"), "isi-market-test")
        self.assertEqual(legacy_app._company_slug("  A&M Corporation  "), "a-m-corporation")
        self.assertEqual(legacy_app._company_slug(""), "unnamed")

    def test_s3_prefix_uses_dash_slug_not_underscore(self):
        prefix = legacy_app._upload_s3_prefix("up123", "ISI Market Test", "gsearch")
        self.assertEqual(prefix, "isi-market-test/gsearch/up123")

    def test_state_key_uses_dash_slug(self):
        key = legacy_app._state_s3_key("up123", "ISI Market Test", "gsearch")
        self.assertEqual(key, "isi-market-test/gsearch/up123/state.json")
