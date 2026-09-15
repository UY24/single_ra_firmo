"""A row that scraped fine but never got an LLM result must be nameable and retryable.

Before this it was neither: it landed in notEnriched.csv reading "No firmographics
extracted." — byte-identical to a row Google simply had no AI Overview for, which is FINAL
— was counted in `no_ai_overview` despite HAVING had an overview, never appeared in
retry.csv, and `retry-failed-rows` reported "Enqueued 0" while correctly redoing it.
"""
import unittest

from app.services.serpwow.reporting import retry_row


class RetryRowLlmIncompleteTests(unittest.TestCase):
    HEADER = ["website_url", "company_name"]
    ORIGINAL = {"website_url": "https://acme.com", "company_name": "Acme", "row_index": 0}

    def _row(self, **kwargs):
        return retry_row(self.ORIGINAL, self.HEADER, "retry_reason",
                         attempts=1, credits=10, **kwargs)

    def test_an_unjudged_row_is_listed_for_rerun(self) -> None:
        row = self._row(llm_incomplete=True)
        self.assertIsNotNone(row, "scraped-but-unjudged row missing from retry.csv")
        self.assertIn("llm_incomplete", row["retry_reason"])
        self.assertIn("no provider re-spend", row["retry_reason"])
        # It carries the input cells verbatim, like every other retry reason, so the file
        # can be re-uploaded as-is.
        self.assertEqual(row["website_url"], "https://acme.com")
        self.assertEqual(row["company_name"], "Acme")

    def test_an_answered_row_is_still_excluded(self) -> None:
        self.assertIsNone(self._row(), "a row with a real answer got into retry.csv")

    def test_a_real_error_still_wins_the_reason(self) -> None:
        """Precedence matters: a provider failure is the more actionable reason, and it is
        the one that carries the refund claim."""
        row = self._row(error="scrape.do 500", llm_incomplete=True)
        self.assertIn("error: scrape.do 500", row["retry_reason"])
        self.assertNotIn("llm_incomplete", row["retry_reason"])

    def test_billed_empty_still_wins_the_reason(self) -> None:
        row = self._row(billed_empty=True, llm_incomplete=True)
        self.assertIn("billed_empty", row["retry_reason"])
        self.assertNotIn("llm_incomplete", row["retry_reason"])


if __name__ == "__main__":
    unittest.main()
