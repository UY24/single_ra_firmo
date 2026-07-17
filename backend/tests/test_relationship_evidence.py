import unittest

from app.services.serpwow.modes.relationship import (
    _extract_https_urls,
    _overview_sources,
    _overview_text,
)


class TestRelationshipOverviewEvidence(unittest.TestCase):
    def test_flattens_all_readable_content_in_order(self):
        raw = {
            "ai_overview": {
                "ai_overview_contents": [
                    {"type": "header", "text": "Relationship"},
                    {"type": "paragraph", "text": "  Eastlink funded Modal.\n"},
                    {"type": "list", "list": [
                        {"text": "Investor: Eastlink Capital"},
                        {"snippet": "Official website: https://modal.com/.",
                         "list": [{"text": "Verified in Series A announcement"}]},
                    ]},
                    {"type": "metadata", "ignored": "do not include me"},
                ]
            }
        }
        self.assertEqual(
            _overview_text(raw),
            "Relationship\nEastlink funded Modal.\n"
            "1. Investor: Eastlink Capital\n"
            "2. Official website: https://modal.com/.\n"
            "2.1. Verified in Series A announcement",
        )

    def test_extracts_compact_sources(self):
        raw = {"ai_overview": {"ai_overview_sources": [
            {"source_name": "TechCrunch", "source_url": "https://techcrunch.com/a"},
            {"source_title": "Portfolio", "source_url": "https://eastlinkcap.com/p"},
            {"source_name": "Missing URL"},
        ]}}
        self.assertEqual(_overview_sources(raw), [
            {"name": "TechCrunch", "url": "https://techcrunch.com/a"},
            {"name": "Portfolio", "url": "https://eastlinkcap.com/p"},
        ])

    def test_extracts_and_deduplicates_typed_https_urls(self):
        text = (
            "Official: https://modal.com/. Duplicate https://modal.com/ and "
            "X: https://eastlinkcap.com/portfolio). Ignore http://legacy.test."
        )
        self.assertEqual(_extract_https_urls(text), [
            "https://modal.com/",
            "https://eastlinkcap.com/portfolio",
        ])


if __name__ == "__main__":
    unittest.main()
