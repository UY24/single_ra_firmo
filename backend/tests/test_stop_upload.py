# POST /uploads/{id}/stop — halts a running SerpWow upload.
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.services.serpwow import engine

PENDING = "Pending Gemini batch post-processing decision."


# gsearch, not relationship: relationship runs have no state.json and no rows — their
# stop is an S3 marker handled by the pointer branch, covered in test_relationship_endpoint.
def _state(rows, gemini_batch=None, pipeline="gsearch"):
    state = {
        "upload_id": "u1", "company_id": "c1", "company_name": "Acme",
        "pipeline": pipeline, "phase": "all", "status": "processing",
        "created_at": "t", "updated_at": "t",
        "total_rows": len(rows), "processed_rows": 0,
        "success_rows": 0, "failed_rows": 0,
        "rows": rows,
    }
    if gemini_batch is not None:
        state["gemini_batch"] = gemini_batch
    return state


def _row(idx, status, error=None, official=None):
    return {"row_index": idx, "company_name": f"y{idx}", "country": "",
            "status": status, "error": error, "status_updated_at": "t",
            "processing_started_at": None,
            "result": {"official_website": official} if official or error == PENDING else None}


class TestStopUpload(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(engine.app)

    def _post(self, state):
        with patch.object(engine, "read_upload_artifact", AsyncMock(return_value=state)), \
             patch.object(engine, "persist_upload_state", AsyncMock()) as persist, \
             patch.object(engine, "_gemini_batch_cancel_sync") as cancel:
            resp = self.client.post("/uploads/u1/stop")
        return resp, persist, cancel

    def test_stops_queued_and_processing_rows(self):
        state = _state([_row(1, "queued"), _row(2, "processing"), _row(3, "completed")])
        resp, persist, _ = self._post(state)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["stopped_rows"], 2)
        self.assertFalse(body["batch_cancelled"])
        persisted = persist.call_args[0][1]
        statuses = {r["row_index"]: (r["status"], r["error"]) for r in persisted["rows"]}
        self.assertEqual(statuses[1], ("failed", "Stopped by user."))
        self.assertEqual(statuses[2], ("failed", "Stopped by user."))
        self.assertEqual(statuses[3][0], "completed")
        self.assertTrue(persisted.get("stopped_by_user_at"))

    def test_pending_batch_rows_are_force_failed_and_chunks_cancelled(self):
        gb = {"status": "running",
              "chunks": [{"chunk_id": 0, "job_name": "batches/abc"}]}
        state = _state([_row(1, "completed", error=PENDING)], gemini_batch=gb)
        resp, persist, cancel = self._post(state)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["batch_cancelled"])
        self.assertEqual(body["stopped_rows"], 1)
        cancel.assert_called_once_with("batches/abc")
        persisted = persist.call_args[0][1]
        self.assertEqual(persisted["gemini_batch"]["status"], "failed")
        self.assertEqual(persisted["gemini_batch"]["error"], "Stopped by user.")
        row = persisted["rows"][0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("Stopped by user", row["error"])

    def test_terminal_upload_is_409(self):
        state = _state([_row(1, "completed", official="https://a.com/"),
                        _row(2, "failed", error="x")])
        resp, _, _ = self._post(state)
        self.assertEqual(resp.status_code, 409)

    def test_missing_upload_is_404(self):
        with patch.object(engine, "read_upload_artifact",
                          AsyncMock(side_effect=FileNotFoundError())):
            resp = self.client.post("/uploads/nope/stop")
        self.assertEqual(resp.status_code, 404)


class TestStopGuards(unittest.TestCase):
    def test_maybe_start_skips_stopped_uploads(self):
        state = {"pipeline": "gsearch", "status": "completed_with_errors",
                 "stopped_by_user_at": "t", "rows": []}
        with patch.dict("os.environ", {"LLM_BATCH": "true"}), \
             patch.object(engine, "persist_upload_state", AsyncMock()) as persist:
            asyncio.run(engine.maybe_start_gemini_batch_for_upload("u-stop", state))
        persist.assert_not_awaited()
        self.assertNotIn("u-stop", engine.gemini_batch_tasks)
        self.assertNotIn("gemini_batch", state)


if __name__ == "__main__":
    unittest.main()
