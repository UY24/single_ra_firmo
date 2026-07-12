import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi.testclient import TestClient

from app.services.serpwow import engine


def batch_state():
    return {
        "upload_id": "upload /1",
        "company_name": "Acme",
        "pipeline": "gsearch",
        "status": "completed",
        "rows": [{
            "row_index": 1,
            "company_name": "Acme",
            "country": "US",
            "status": "completed",
            "result": {"official_website": "https://acme.example"},
        }],
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
                self.assertEqual(persist.await_args.args[1]["batch_deleted_by_user_at"], "now")
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

    def test_cancel_requested_remains_pending_for_reporting_pipelines(self):
        for pipeline in ("gsearch", "gmaps", "relationship"):
            with self.subTest(pipeline=pipeline), \
                 patch.object(engine, "_batch_postprocess_enabled_for", return_value=True):
                state = {"pipeline": pipeline, "gemini_batch": {"status": "cancel_requested"}}
                self.assertTrue(engine._batch_postprocess_pending(state))

    def test_explicit_retry_clears_user_deletion_tombstone(self):
        state = batch_state()
        state["rows"][0]["status"] = "failed"
        state["rows"][0]["error"] = "deleted batch"
        state["batch_deleted_by_user_at"] = "deleted-at"
        state["gemini_batch"]["deleted_by_user_at"] = "deleted-at"
        with patch.object(engine, "rabbitmq_exchange", MagicMock()), \
             patch.object(engine, "get_upload_state", AsyncMock(return_value=state)), \
             patch.object(engine, "read_upload_artifact", AsyncMock(return_value=state)), \
             patch.object(engine, "persist_upload_state", AsyncMock()) as persist, \
             patch.object(engine, "publish_job", AsyncMock()), \
             patch.object(engine, "_batch_postprocess_enabled_for", return_value=True):
            response = self.client.post("/uploads/upload-1/retry-failed-rows")
        self.assertEqual(response.status_code, 200, response.text)
        persisted = persist.await_args.args[1]
        self.assertNotIn("batch_deleted_by_user_at", persisted)
        self.assertNotIn("deleted_by_user_at", persisted["gemini_batch"])
        self.assertEqual(persisted["gemini_batch"]["status"], "waiting_for_rows")


class TestBatchJobActionRealPersistence(unittest.IsolatedAsyncioTestCase):
    async def _sync_with_real_persist(self, action):
        state = batch_state()
        write_artifact = AsyncMock()
        finalize = AsyncMock()
        notify_terminal = Mock()
        run_batch = AsyncMock()
        with patch.object(engine, "read_upload_artifact", AsyncMock(return_value=state)), \
             patch.object(engine, "write_upload_artifact", write_artifact), \
             patch.object(engine, "update_summary_cache"), \
             patch.object(engine, "build_upload_output_payload", return_value={}), \
             patch.object(engine, "_batch_postprocess_enabled_for", return_value=True), \
             patch.object(engine, "_finalize_serpwow_outputs", finalize), \
             patch.object(engine, "_notify_slack_terminal", notify_terminal), \
             patch.object(engine, "run_gemini_batch_for_upload", run_batch):
            updated = await engine._sync_batch_job_action_local_state(
                "upload /1", "jobs/top", action)
            await asyncio.sleep(0)
        task = engine.gemini_batch_tasks.pop("upload /1", None)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return state, updated, finalize, notify_terminal, run_batch

    async def test_cancel_real_persist_defers_terminal_side_effects(self):
        state, updated, finalize, notify_terminal, run_batch = \
            await self._sync_with_real_persist("cancel")
        self.assertTrue(updated)
        self.assertEqual(state["gemini_batch"]["status"], "cancel_requested")
        finalize.assert_not_awaited()
        notify_terminal.assert_not_called()
        run_batch.assert_not_awaited()

    async def test_delete_real_persist_does_not_relaunch_batch(self):
        state, updated, _finalize, _notify_terminal, run_batch = \
            await self._sync_with_real_persist("delete")
        self.assertTrue(updated)
        self.assertEqual(state["gemini_batch"]["status"], "failed")
        self.assertTrue(state["gemini_batch"].get("deleted_by_user_at"))
        self.assertTrue(state.get("batch_deleted_by_user_at"))
        self.assertNotIn("upload /1", engine.gemini_batch_tasks)
        run_batch.assert_not_awaited()

    async def test_inflight_driver_does_not_overwrite_delete_marker(self):
        state = batch_state()
        state["gemini_batch"]["status"] = "queued"
        persisted = {"state": state}

        async def fake_persist(_upload_id, latest):
            persisted["state"] = latest

        async def delete_while_chunk_runs(*_args):
            updated = await engine._sync_batch_job_action_local_state(
                "upload /1", "jobs/top", "delete")
            self.assertTrue(updated)
            return {
                "chunk_id": 0,
                "job_name": "jobs/chunk 0",
                "status": "succeeded",
                "error": None,
                "parsed_by_row": {1: {"official_website": "https://changed.example"}},
                "usage": {},
            }

        with patch.object(
            engine, "read_upload_artifact",
            AsyncMock(side_effect=lambda _uid, _name: persisted["state"]),
        ), patch.object(
            engine, "persist_upload_state", side_effect=fake_persist,
        ), patch.object(
            engine, "_run_one_gemini_chunk", side_effect=delete_while_chunk_runs,
        ), patch.object(engine, "_now_iso", return_value="now"):
            await engine.run_gemini_batch_for_upload("upload /1")

        batch = persisted["state"]["gemini_batch"]
        self.assertEqual(batch["status"], "failed")
        self.assertEqual(batch["deleted_by_user_at"], "now")
        self.assertEqual(persisted["state"]["batch_deleted_by_user_at"], "now")
        self.assertEqual(
            persisted["state"]["rows"][0]["result"]["official_website"],
            "https://acme.example",
        )

    async def test_explicit_retry_waits_for_deleted_generation_to_exit(self):
        state = batch_state()
        state["rows"][0]["status"] = "failed"
        state["batch_deleted_by_user_at"] = "deleted-at"
        state["gemini_batch"]["deleted_by_user_at"] = "deleted-at"
        old_driver_exited = asyncio.Event()

        async def old_driver():
            try:
                await asyncio.Event().wait()
            finally:
                old_driver_exited.set()

        old_task = asyncio.create_task(old_driver())
        await asyncio.sleep(0)
        engine.gemini_batch_tasks["upload-1"] = old_task
        persist = AsyncMock()
        try:
            with patch.object(engine, "rabbitmq_exchange", MagicMock()), \
                 patch.object(engine, "get_upload_state", AsyncMock(return_value=state)), \
                 patch.object(engine, "read_upload_artifact", AsyncMock(return_value=state)), \
                 patch.object(engine, "persist_upload_state", persist), \
                 patch.object(engine, "publish_job", AsyncMock()), \
                 patch.object(engine, "_batch_postprocess_enabled_for", return_value=True):
                await engine.retry_failed_rows("upload-1", limit=0)
            self.assertTrue(old_driver_exited.is_set())
            self.assertTrue(old_task.cancelled())
            persisted = persist.await_args.args[1]
            self.assertNotIn("batch_deleted_by_user_at", persisted)
        finally:
            engine.gemini_batch_tasks.pop("upload-1", None)
            if not old_task.done():
                old_task.cancel()
                await asyncio.gather(old_task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
