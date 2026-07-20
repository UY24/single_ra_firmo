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

    def test_out_of_scope_pipeline_keeps_failed_for_notfound(self):
        # firmographics: no serpwow_reporting remap — not_found stays "failed"
        info, status = engine._finalize_row_outcome(_result(phases=[{"used": True}]),
                                                     pipeline="firmographics", batch_postprocess_enabled=False)
        self.assertEqual(status, "failed")

    def test_found_is_completed(self):
        info, status = engine._finalize_row_outcome(_result(official="https://x.com"),
                                                     pipeline="relationship", batch_postprocess_enabled=False)
        self.assertEqual((info.outcome, status), (o.OUTCOME_FOUND, "completed"))


if __name__ == "__main__":
    unittest.main()
