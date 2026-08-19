import unittest
from app.services.serpwow import engine
from app.services.serpwow import outcomes as o


def _result(official=None, phases=None, row_error=None, skip_llm=False):
    fr = [{"phase": "p", "success": p.get("used", False), "error": p.get("error"),
           "error_category": p.get("error_category")} for p in (phases or [])]
    return {"official_website": official,
            "context": {"formatted_results": fr, "row_error": row_error, "skip_llm": skip_llm}}


class TestFinalizeOutcome(unittest.TestCase):
    def test_notfound_is_completed_for_gsearch(self):
        info, status = engine._finalize_row_outcome(_result(phases=[{"used": True}]),
                                                     pipeline="gsearch", batch_postprocess_enabled=False)
        self.assertEqual(info.outcome, o.OUTCOME_NOT_FOUND)
        self.assertEqual(status, "completed")

    def test_all_serpwow_errored_is_error_failed(self):
        info, status = engine._finalize_row_outcome(
            _result(phases=[{"used": False, "error": "429", "error_category": o.CAT_RATE_LIMIT}]),
            pipeline="gmaps", batch_postprocess_enabled=False)
        self.assertEqual((info.outcome, info.error_source, status),
                         (o.OUTCOME_ERROR, o.SRC_SERPWOW, "failed"))

    def test_firmographics_notfound_is_completed_not_failed(self):
        """A website with no AI overview is a business not-found, not a failure.

        It was "failed" until 2026-08-19, which put it under "View failed rows" and made
        it eligible for a "Rerun failed" that would re-buy the same empty answer at 10
        scrape.do credits a row.
        """
        info, status = engine._finalize_row_outcome(
            _result(official="https://x.com", phases=[{"used": True, "success": True}]),
            pipeline="firmographics", batch_postprocess_enabled=False)
        self.assertEqual((info.outcome, status), (o.OUTCOME_NOT_FOUND, "completed"))

    def test_firmographics_echoed_input_url_is_not_found(self):
        """official_website is the INPUT for this pipeline, so it cannot prove success."""
        info, status = engine._finalize_row_outcome(
            _result(official="https://x.com"),
            pipeline="firmographics", batch_postprocess_enabled=False)
        self.assertEqual(info.outcome, o.OUTCOME_NOT_FOUND)

    def test_firmographics_provider_error_is_error(self):
        info, status = engine._finalize_row_outcome(
            _result(official="https://x.com",
                    phases=[{"success": False, "error": "429",
                             "error_source": o.SRC_SCRAPEDO,
                             "error_category": o.CAT_RATE_LIMIT}]),
            pipeline="firmographics", batch_postprocess_enabled=False)
        self.assertEqual((info.outcome, info.error_source, status),
                         (o.OUTCOME_ERROR, o.SRC_SCRAPEDO, "failed"))

    def test_found_is_completed(self):
        info, status = engine._finalize_row_outcome(_result(official="https://x.com"),
                                                     pipeline="relationship", batch_postprocess_enabled=False)
        self.assertEqual((info.outcome, status), (o.OUTCOME_FOUND, "completed"))


if __name__ == "__main__":
    unittest.main()
