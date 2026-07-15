"""Regression test: FIX 1 — batch-mode gsearch prompt must see the full candidate list.

When a gsearch row's context["candidates"] contains multiple URLs, calling
_build_batch_prompt_for_row(row) must include all of them in the rendered
prompt text, not just the single official_website stored on the result.
"""
import unittest

from app.services.serpwow import engine as legacy_app


class TestBatchPromptIncludesAllCandidates(unittest.TestCase):
    """_build_batch_prompt_for_row folds context["candidates"] into the dedup set."""

    def _make_row(self, candidates, official_website=None):
        return {
            "row_index": 0,
            "company_name": "Acme Corp",
            "country": "us",
            "industry": "manufacturing",
            "full_address": "123 Main St",
            "firm_id": "firm-001",
            "result": {
                "official_website": official_website or candidates[0],
                "context": {
                    "candidates": candidates,
                    "search_attempts": [],
                    "serpwow": {},
                    "gmaps": {},
                    "ai": {},
                },
            },
        }

    def test_all_candidate_urls_appear_in_prompt(self):
        """All three candidate URLs must be present in the batch prompt."""
        candidates = [
            "https://a-co.com",
            "https://b-co.com",
            "https://c-co.com",
        ]
        row = self._make_row(candidates, official_website="https://a-co.com")
        prompt = legacy_app._build_batch_prompt_for_row(row)

        for url in candidates:
            self.assertIn(url, prompt, f"Prompt missing candidate: {url}")

    def test_prompt_has_more_than_one_candidate(self):
        """The Candidate URLs section must list more than one URL."""
        candidates = [
            "https://a-co.com",
            "https://b-co.com",
            "https://c-co.com",
        ]
        row = self._make_row(candidates, official_website="https://a-co.com")
        prompt = legacy_app._build_batch_prompt_for_row(row)

        # The prompt serialises the deduped list as JSON; check >1 entry appears
        # by confirming at least two of the candidate strings are in the prompt.
        found = [url for url in candidates if url in prompt]
        self.assertGreater(len(found), 1,
                           f"Expected >1 candidates in prompt, found {found!r}")

    def test_no_regression_for_full_pipeline_row(self):
        """A row without context["candidates"] (full pipeline) must still work.

        context["candidates"] is absent for full-pipeline rows; the function
        must behave identically to before FIX 1 (no KeyError, prompt is a string).
        """
        row = {
            "row_index": 0,
            "company_name": "Beta Ltd",
            "country": "gb",
            "industry": "finance",
            "full_address": "",
            "firm_id": None,
            "result": {
                "official_website": "https://beta.co.uk",
                "context": {
                    # No "candidates" key — simulates full-pipeline context
                    "search_attempts": [
                        {"official_website": "https://beta.co.uk"}
                    ],
                    "serpwow": {},
                    "gmaps": {},
                    "ai": {},
                },
            },
        }
        prompt = legacy_app._build_batch_prompt_for_row(row)
        self.assertIsInstance(prompt, str)
        self.assertIn("https://beta.co.uk", prompt)


if __name__ == "__main__":
    unittest.main()
