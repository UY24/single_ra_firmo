import unittest
from app.services.serpwow import engine
from app.services.serpwow import outcomes as o


def _state(rows, pipeline="gsearch"):
    return {"pipeline": pipeline, "rows": rows}


class TestSummarizeOutcome(unittest.TestCase):
    def test_outcome_counts(self):
        rows = [
            {"status": "completed", "outcome": o.OUTCOME_FOUND},
            {"status": "completed", "outcome": o.OUTCOME_NOT_FOUND},
            {"status": "failed", "outcome": o.OUTCOME_ERROR, "error_source": o.SRC_SERPWOW,
             "error_category": o.CAT_RATE_LIMIT},
        ]
        s = engine.summarize_upload_state(_state(rows))
        self.assertEqual(s["outcome_counts"], {"found": 1, "not_found": 1, "errored": 1})
        self.assertEqual(s["failed_rows"], 1)
        self.assertEqual(s["status"], "completed_with_errors")

    def test_all_notfound_is_completed_not_error(self):
        rows = [{"status": "completed", "outcome": o.OUTCOME_NOT_FOUND},
                {"status": "completed", "outcome": o.OUTCOME_NOT_FOUND}]
        s = engine.summarize_upload_state(_state(rows))
        self.assertEqual(s["status"], "completed")
        self.assertEqual(s["failed_rows"], 0)

    def test_failure_analysis_by_source(self):
        rows = [{"row_index": 63, "company_name": "Kitche", "status": "failed",
                 "outcome": o.OUTCOME_ERROR, "error_source": o.SRC_GEMINI,
                 "error_category": o.CAT_TIMEOUT, "error": "boom"}]
        fa = engine.build_failure_analysis(_state(rows))
        self.assertEqual(fa["by_source"], [{"source": "gemini", "count": 1}])
        self.assertEqual(fa["by_category"], [{"category": "timeout", "count": 1}])
        self.assertEqual(fa["sample_failed_rows"][0]["error_source"], "gemini")
        self.assertEqual(fa["sample_failed_rows"][0]["error_category"], "timeout")


if __name__ == "__main__":
    unittest.main()
