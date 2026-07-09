"""TDD for Task 4: batch-apply + reconciler paths set outcome/error fields.

Covers the direct-write sites that don't go through update_row_state:
  - _apply_batch_parsed_to_row (found / no-website)
  - _apply_relationship_batch_parsed_to_row (gated / not-confirmed)
  - reconcile_stuck_gsearch_rows force-fail (via source inspection, since a full
    integration test needs heavy RabbitMQ/upload-state scaffolding)
"""
import inspect
import unittest

from app.services.serpwow import engine
from app.services.serpwow import outcomes as o


class TestBatchApplyOutcome(unittest.TestCase):
    def test_batch_no_website_is_notfound_completed(self):
        row = {"company_name": "X", "result": {"context": {}}}
        status = engine._apply_batch_parsed_to_row(row, {"official_website": None}, {}, "gemini-x")
        self.assertEqual(status, "completed")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["outcome"], o.OUTCOME_NOT_FOUND)
        self.assertIsNone(row["error_source"])
        self.assertIsNone(row["error_category"])
        self.assertEqual(row["error"], o.BATCH_NOT_FOUND)

    def test_batch_found(self):
        # NOTE: not "https://x.com" as in the task brief's illustrative snippet --
        # x.com is itself in url_utils.is_disallowed_official_url's blocklist (it's
        # Twitter/X's domain), which would make selected_url get discarded and this
        # test would spuriously exercise the not-found branch instead of found.
        row = {"company_name": "Acme Example", "result": {"context": {}}}
        status = engine._apply_batch_parsed_to_row(
            row, {"official_website": "https://acme-example.com"}, {}, "gemini-x")
        self.assertEqual((status, row["outcome"]), ("completed", o.OUTCOME_FOUND))
        self.assertEqual(row["status"], "completed")
        self.assertIsNone(row["error_source"])
        self.assertIsNone(row["error_category"])
        self.assertIsNone(row["error"])

    def test_relationship_not_confirmed_is_notfound_completed(self):
        row = {"company_name": "Y", "result": {"context": {"pipeline": "relationship",
               "candidates": [], "x_domain": ""}}}
        status = engine._apply_batch_parsed_to_row(
            row, {"relationship_status": "not_confirmed"}, {}, "gemini-x")
        self.assertEqual(status, "completed")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["outcome"], o.OUTCOME_NOT_FOUND)
        self.assertIsNone(row["error_source"])
        self.assertIsNone(row["error_category"])

    def test_relationship_confirmed_is_found_completed(self):
        row = {"company_name": "Y", "result": {"context": {"pipeline": "relationship",
               "candidates": ["https://y.com"], "x_domain": "x.com"}}}
        status = engine._apply_batch_parsed_to_row(
            row,
            {"relationship_status": "confirmed", "gated_url": "https://y.com",
             "official_website": "https://y.com"},
            {},
            "gemini-x",
        )
        self.assertEqual(status, "completed")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["outcome"], o.OUTCOME_FOUND)


class TestReconcilerForceFailSetsErrorFields(unittest.TestCase):
    """The reconciler force-fail sites can't easily be exercised without heavy
    RabbitMQ/upload-state scaffolding, so assert directly on the source that
    both force-fail branches set outcome=error/server/timeout alongside status=failed."""

    def test_force_fail_sites_set_outcome_error_server_timeout(self):
        src = inspect.getsource(engine.reconcile_stuck_gsearch_rows)
        self.assertEqual(src.count('row["status"] = "failed"'), 2,
                          "expected exactly the two force-fail sites")
        self.assertEqual(src.count("_outcomes.OUTCOME_ERROR"), 2)
        self.assertEqual(src.count("_outcomes.SRC_SERVER"), 2)
        self.assertEqual(src.count("_outcomes.CAT_TIMEOUT"), 2)


if __name__ == "__main__":
    unittest.main()
