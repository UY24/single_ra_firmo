import copy
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.services.serpwow import engine


def batch_state():
    return {
        "upload_id": "upload /1",
        "status": "completed",
        "gemini_batch": {
            "status": "running",
            "job_name": "jobs/top",
            "chunks": [
                {"chunk_id": 0, "job_name": "jobs/chunk 0", "status": "running", "error": None},
                {"chunk_id": 1, "job_name": "jobs/chunk 1", "status": "running", "error": None},
            ],
            "error": None,
        },
    }


class TestBatchJobActionLocalSync(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(engine.app)

    def _post(self, action, job_name, state):
        remote = "_gemini_batch_cancel_sync" if action == "cancel" else "_gemini_batch_delete_sync"
        with patch.object(engine, remote, return_value={"ok": True}), \
             patch.object(engine, "read_upload_artifact", AsyncMock(return_value=state)), \
             patch.object(engine, "persist_upload_state", AsyncMock()) as persist, \
             patch.object(engine, "_now_iso", return_value="now"):
            response = self.client.post(
                f"/batch/jobs/{action}",
                params={"job_name": job_name, "upload_id": "upload /1"},
            )
        return response, persist

    def test_cancel_matching_top_and_chunk_updates_local_state(self):
        for job_name in ("jobs/top", "jobs/chunk 0"):
            with self.subTest(job_name=job_name):
                response, persist = self._post("cancel", job_name, copy.deepcopy(batch_state()))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json().get("local_state_updated"))
                persisted = persist.await_args.args[1]
                self.assertEqual(persisted["gemini_batch"]["status"], "cancel_requested")
                chunk = persisted["gemini_batch"]["chunks"][0]
                expected = "cancel_requested" if job_name == "jobs/chunk 0" else "running"
                self.assertEqual(chunk["status"], expected)

    def test_delete_matching_top_and_chunk_removes_local_job_recovery_state(self):
        for job_name in ("jobs/top", "jobs/chunk 0"):
            with self.subTest(job_name=job_name):
                response, persist = self._post("delete", job_name, copy.deepcopy(batch_state()))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json().get("local_state_updated"))
                batch = persist.await_args.args[1]["gemini_batch"]
                self.assertEqual(batch["status"], "failed")
                self.assertEqual(batch["completed_at"], "now")
                self.assertEqual(batch["error"], "Remote batch deleted by user")
                if job_name == "jobs/top":
                    self.assertNotIn("job_name", batch)
                else:
                    chunk = batch["chunks"][0]
                    self.assertNotIn("job_name", chunk)
                    self.assertEqual(chunk["status"], "failed")
                    self.assertEqual(chunk["error"], "Remote batch deleted by user")

    def test_mismatched_job_keeps_local_state_unchanged(self):
        for action in ("cancel", "delete"):
            with self.subTest(action=action):
                state = copy.deepcopy(batch_state())
                response, persist = self._post(action, "jobs/other", state)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIs(response.json().get("local_state_updated"), False)
                persist.assert_not_awaited()
                self.assertEqual(state, batch_state())

    def test_missing_upload_retains_remote_only_behavior(self):
        with patch.object(engine, "_gemini_batch_cancel_sync", return_value={"ok": True}), \
             patch.object(engine, "read_upload_artifact", AsyncMock(side_effect=FileNotFoundError)), \
             patch.object(engine, "persist_upload_state", AsyncMock()) as persist:
            response = self.client.post(
                "/batch/jobs/cancel",
                params={"job_name": "jobs/top", "upload_id": "missing"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json().get("local_state_updated"), False)
        persist.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
