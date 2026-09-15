"""TDD for Task 4: batch-apply + reconciler paths set outcome/error fields.

Covers the direct-write sites that don't go through update_row_state:
  - _apply_batch_parsed_to_row (found / no-website)
  - reconcile_stuck_gsearch_rows force-fail (via source inspection, since a full
    integration test needs heavy RabbitMQ/upload-state scaffolding)
"""
import inspect
import json
import unittest
from unittest import mock

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


class TestBatchDriverDoesNotCorruptSkipLlmRows(unittest.IsolatedAsyncioTestCase):
    """Regression (Fix 1): a skip_llm short-circuit row, already finalized
    status=completed/outcome=not_found by the worker and deliberately NEVER seeded
    into the batch, must not be force-failed to error/gemini by the driver's
    chunk-failure sweep just because some OTHER row in the same upload went through
    the batch."""

    async def test_rows_outside_snapshot_follow_their_existing_state(self):
        # Row 1: skip_llm short-circuit -> already completed/not_found by the worker.
        skip_row = {
            "row_index": 1, "company_name": "NoEvidenceCorp", "country": "",
            "status": "completed", "outcome": o.OUTCOME_NOT_FOUND,
            "error_source": None, "error_category": None,
            "error": "No candidate URLs found.",
            "result": {"official_website": None,
                       "context": {"pipeline": "gsearch", "skip_llm": True,
                                   "candidates": []}},
        }
        # Row 2: normal row that goes through the batch and gets a URL.
        batch_row = {
            "row_index": 2, "company_name": "Modal", "country": "",
            "status": "completed",
            "error": "Pending Gemini batch post-processing decision.",
            "result": {"official_website": None, "gemini_cost_usd": 0.0, "total_cost_usd": 0.0,
                       "context": {"pipeline": "gsearch", "skip_llm": False,
                                   "candidates": ["https://modal.com/"],
                                   "cost_breakdown": {"serpwow_request_count": 1}}},
        }
        state = {"upload_id": "mix-u1", "company_name": "Co", "pipeline": "gsearch",
                 "status": "completed_with_errors", "rows": [skip_row, batch_row],
                 "gemini_batch": {"status": "queued", "chunks": []}}
        persisted = {"state": state}
        late_row = {
            "row_index": 100, "company_name": "Late Corp", "country": "",
            "status": "completed",
            "error": "Pending Gemini batch post-processing decision.",
            "result": {"official_website": None, "context": {
                "pipeline": "gsearch", "skip_llm": False,
                "candidates": ["https://late.example/"],
            }},
        }

        async def fake_persist(uid, st):
            persisted["state"] = st

        def fake_create(model, items, display_name):
            return {"name": "jobs/OK", "_keys": [k for k, _ in items]}

        def fake_get(name):
            return {"name": name, "done": True, "state": {"name": "JOB_STATE_SUCCEEDED"}}

        def fake_collect(obj):
            # Row 100 completes SerpWow after the batch input snapshot was built.
            persisted["state"]["rows"].append(late_row)
            # Resolve the batched row (row-2) to an in-candidate URL.
            out = []
            for k in obj.get("_keys", []):
                out.append({"key": k, "text": json.dumps(
                    {"official_website": "https://modal.com/",
                     "confidence_score": 90}), "usage": {}})
            return out

        with mock.patch.dict("os.environ", {"GEMINI_BATCH_SHARD_SIZE": "100",
                                            "GEMINI_BATCH_MAX_INFLIGHT": "5",
                                            "GEMINI_API_KEY": "k"}, clear=False), \
             mock.patch.object(engine, "read_upload_artifact",
                               new=mock.AsyncMock(side_effect=lambda u, k: persisted["state"])), \
             mock.patch.object(engine, "persist_upload_state", new=fake_persist), \
             mock.patch.object(engine, "write_upload_text_artifact", new=mock.AsyncMock()), \
             mock.patch("app.services.ai_mode.gemini_batch.create_batch", side_effect=fake_create), \
             mock.patch("app.services.ai_mode.gemini_batch.get_batch", side_effect=fake_get), \
             mock.patch("app.services.ai_mode.gemini_batch.collect_results", side_effect=fake_collect):
            await engine.run_gemini_batch_for_upload("mix-u1")

        rows = {r["row_index"]: r for r in persisted["state"]["rows"]}
        # The skip_llm row must be untouched: still completed / not_found / no error source.
        self.assertEqual(rows[1]["status"], "completed")
        self.assertEqual(rows[1]["outcome"], o.OUTCOME_NOT_FOUND)
        self.assertIsNone(rows[1]["error_source"])
        self.assertIsNone(rows[1]["error_category"])
        # The batched row was confirmed -> found.
        self.assertEqual(rows[2]["status"], "completed")
        self.assertEqual(rows[2]["result"]["official_website"], "https://modal.com/")
        self.assertEqual(rows[100]["status"], "failed")
        self.assertEqual(rows[100]["outcome"], o.OUTCOME_ERROR)
        self.assertEqual(rows[100]["error_source"], o.SRC_GEMINI)
        self.assertEqual(rows[100]["error_category"], o.CAT_INTERNAL)
        self.assertIn("missed", rows[100]["error"])
        self.assertIn("snapshot", rows[100]["error"])


if __name__ == "__main__":
    unittest.main()
