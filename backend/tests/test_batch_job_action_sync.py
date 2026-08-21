import asyncio
import os
import copy
import tempfile
import unittest
from pathlib import Path
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

    def _post(self, action, job_name, state, expected_generation=None):
        remote = "_gemini_batch_cancel_sync" if action == "cancel" else "_gemini_batch_delete_sync"

        async def fake_tombstone(_upload_id, _state, deleted_job, deleted_at, *, top_level_deleted):
            return {
                "deleted_by_user_at": deleted_at,
                "job_names": [deleted_job],
                "top_level_deleted": top_level_deleted,
            }

        with patch.object(engine, remote, return_value={"ok": True}), \
             patch.object(engine, "read_upload_artifact", AsyncMock(return_value=state)), \
             patch.object(engine, "persist_upload_state", AsyncMock()) as persist, \
             patch.object(engine, "_write_batch_deletion_tombstone", side_effect=fake_tombstone), \
             patch.object(engine, "_now_iso", return_value="now"):
            params = {"job_name": job_name, "upload_id": "upload /1"}
            if expected_generation is not None:
                params["expected_generation"] = expected_generation
            response = self.client.post(f"/batch/jobs/{action}", params=params)
        return response, persist

    def test_cancel_matching_top_and_chunk_updates_local_state(self):
        for job_name in ("jobs/top", "jobs/chunk 0"):
            with self.subTest(job_name=job_name):
                response, persist = self._post("cancel", job_name, copy.deepcopy(batch_state()))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json().get("local_state_updated"))
                persisted = persist.await_args.args[1]
                aggregate_expected = "cancel_requested" if job_name == "jobs/top" else "running"
                self.assertEqual(persisted["gemini_batch"]["status"], aggregate_expected)
                chunk = persisted["gemini_batch"]["chunks"][0]
                expected = "cancel_requested" if job_name == "jobs/chunk 0" else "running"
                self.assertEqual(chunk["status"], expected)

    def test_delete_matching_top_and_chunk_removes_local_job_recovery_state(self):
        for job_name in ("jobs/top", "jobs/chunk 0"):
            with self.subTest(job_name=job_name):
                response, persist = self._post("delete", job_name, copy.deepcopy(batch_state()))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json().get("local_state_updated"))
                persisted = persist.await_args.args[1]
                batch = persisted["gemini_batch"]
                if job_name == "jobs/top":
                    self.assertEqual(batch["status"], "failed")
                    self.assertEqual(batch["completed_at"], "now")
                    self.assertEqual(batch["error"], "Remote batch deleted by user")
                    self.assertEqual(persisted["batch_deleted_by_user_at"], "now")
                    self.assertNotIn("job_name", batch)
                else:
                    self.assertEqual(batch["status"], "running")
                    self.assertNotIn("batch_deleted_by_user_at", persisted)
                    chunk = batch["chunks"][0]
                    self.assertNotIn("job_name", chunk)
                    self.assertEqual(chunk["status"], "failed")
                    self.assertEqual(chunk["error"], "Remote batch deleted by user")

    def test_mismatched_cancel_keeps_local_state_unchanged(self):
        state = copy.deepcopy(batch_state())
        response, persist = self._post("cancel", "jobs/other", state)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json().get("local_state_updated"), False)
        persist.assert_not_awaited()
        self.assertEqual(state, batch_state())

    def test_delete_before_chunk_metadata_persists_still_records_tombstone(self):
        state = copy.deepcopy(batch_state())
        response, persist = self._post(
            "delete", "jobs/new-chunk", state, expected_generation=0)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json().get("local_state_updated"))
        persisted = persist.await_args.args[1]
        self.assertIn("jobs/new-chunk", persisted["batch_deleted_job_names"])
        self.assertEqual(persisted["gemini_batch"]["status"], "running")

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
        # relationship is absent: it never routes through the shared row-batch engine
        # (its own Gemini Batch driver lives in relationship_runner).
        for pipeline in ("gsearch",):
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
             patch.object(engine, "_clear_batch_deletion_tombstone", AsyncMock()), \
             patch.object(engine, "persist_upload_state", AsyncMock()) as persist, \
             patch.object(engine, "publish_job", AsyncMock()), \
             patch.object(engine, "_batch_postprocess_enabled_for", return_value=True):
            response = self.client.post("/uploads/upload-1/retry-failed-rows")
        self.assertEqual(response.status_code, 200, response.text)
        persisted = persist.await_args.args[1]
        self.assertNotIn("batch_deleted_by_user_at", persisted)
        self.assertNotIn("deleted_by_user_at", persisted["gemini_batch"])
        self.assertEqual(persisted["gemini_batch"]["status"], "waiting_for_rows")

    def test_batch_jobs_list_correlates_reporting_pipeline_chunks(self):
        summary = {
            "upload_id": "gsearch-upload",
            "pipeline": "gsearch",
            "status": "completed",
            "updated_at": "2026-07-12T00:00:00Z",
            "gemini_batch": {
                "status": "completed_with_errors",
                "job_name": "jobs/top",
                "chunks": [
                    {"chunk_id": 0, "job_name": "jobs/chunk-0", "status": "running"},
                    {"chunk_id": 1, "job_name": "jobs/chunk-1", "status": "cancel_requested"},
                ],
            },
        }
        list_summaries = AsyncMock(return_value=[summary])
        with patch.object(engine, "list_upload_summaries", list_summaries), \
             patch.dict("os.environ", {"ENABLE_REMOTE_GEMINI_BATCH_LIST": "false"}, clear=False):
            response = self.client.get("/batch/jobs")
        self.assertEqual(response.status_code, 200, response.text)
        jobs = {job["job_name"]: job for job in response.json()["jobs"]}
        self.assertEqual(set(jobs), {"jobs/top", "jobs/chunk-0", "jobs/chunk-1"})
        self.assertEqual(jobs["jobs/top"]["batch_status"], "completed_with_errors")
        self.assertEqual(jobs["jobs/chunk-0"]["upload_id"], "gsearch-upload")
        self.assertEqual(jobs["jobs/chunk-0"].get("chunk_id"), 0)
        self.assertEqual(jobs["jobs/chunk-0"]["batch_status"], "running")
        self.assertEqual(jobs["jobs/chunk-1"]["upload_id"], "gsearch-upload")
        self.assertEqual(jobs["jobs/chunk-1"].get("chunk_id"), 1)
        self.assertEqual(jobs["jobs/chunk-1"]["batch_status"], "cancel_requested")
        self.assertIsNone(list_summaries.await_args.kwargs["pipeline"])

    def test_batch_jobs_list_correlates_remote_only_hyphenated_upload_id(self):
        upload_id = "team-alpha-run-123"
        operation = {
            "name": "operations/batch-remote",
            "metadata": {
                "state": "BATCH_STATE_RUNNING",
                "displayName": (
                    "single-ra-upload-team-alpha-run-123-"
                    "123e4567-e89b-12d3-a456-426614174000"
                ),
            },
        }
        engine.gemini_batch_list_cache = []
        engine.gemini_batch_list_cache_fetched_at = 0.0
        engine.gemini_batch_list_error_cooldown_until = 0.0
        with patch.object(engine, "list_upload_summaries", AsyncMock(return_value=[])), \
             patch.object(engine, "_gemini_batch_list_sync", return_value={"operations": [operation]}), \
             patch.dict("os.environ", {"ENABLE_REMOTE_GEMINI_BATCH_LIST": "true"}, clear=False):
            response = self.client.get("/batch/jobs")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["jobs"][0]["upload_id"], upload_id)

    def test_batch_jobs_list_carries_generation_for_pre_persist_remote_job(self):
        upload_id = "team-alpha-run-123"
        summary = {
            "upload_id": upload_id,
            "pipeline": "gsearch",
            "status": "completed",
            "gemini_batch": {
                "status": "waiting_for_rows",
                "generation": 2,
            },
        }
        operation = {
            "name": "operations/batch-new",
            "metadata": {
                "state": "BATCH_STATE_RUNNING",
                "displayName": "gsearch-team-alpha-run-123-gen2-chunk0",
            },
        }
        engine.gemini_batch_list_cache = []
        engine.gemini_batch_list_cache_fetched_at = 0.0
        engine.gemini_batch_list_error_cooldown_until = 0.0
        with patch.object(
            engine, "list_upload_summaries", AsyncMock(return_value=[summary])
        ), patch.object(
            engine, "_gemini_batch_list_sync", return_value={"operations": [operation]}
        ), patch.dict(
            "os.environ", {"ENABLE_REMOTE_GEMINI_BATCH_LIST": "true"}, clear=False
        ):
            response = self.client.get("/batch/jobs")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["jobs"][0]["upload_id"], upload_id)
        self.assertEqual(response.json()["jobs"][0]["batch_generation"], 2)

    def test_batch_jobs_keeps_stale_remote_operation_generation_after_retry(self):
        upload_id = "team-alpha-run-123"
        summary = {
            "upload_id": upload_id,
            "pipeline": "gsearch",
            "status": "completed",
            "gemini_batch": {
                "status": "waiting_for_rows",
                "generation": 2,
            },
        }
        stale_operation = {
            "name": "operations/batch-old",
            "metadata": {
                "state": "BATCH_STATE_RUNNING",
                "displayName": "gsearch-team-alpha-run-123-gen1-chunk0",
            },
        }
        engine.gemini_batch_list_cache = []
        engine.gemini_batch_list_cache_fetched_at = 0.0
        engine.gemini_batch_list_error_cooldown_until = 0.0
        with patch.object(
            engine, "list_upload_summaries", AsyncMock(return_value=[summary])
        ), patch.object(
            engine,
            "_gemini_batch_list_sync",
            return_value={"operations": [stale_operation]},
        ), patch.dict(
            "os.environ", {"ENABLE_REMOTE_GEMINI_BATCH_LIST": "true"}, clear=False
        ):
            response = self.client.get("/batch/jobs")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["jobs"][0]["upload_id"], upload_id)
        self.assertEqual(response.json()["jobs"][0]["batch_generation"], 1)

    def test_stale_delete_generation_does_not_contaminate_retry_state(self):
        state = batch_state()
        state["gemini_batch"] = {
            "status": "waiting_for_rows",
            "generation": 2,
            "job_name": None,
            "error": None,
        }
        with patch.object(engine, "_gemini_batch_delete_sync", return_value={"ok": True}), \
             patch.object(engine, "read_upload_artifact", AsyncMock(return_value=state)), \
             patch.object(engine, "_write_batch_deletion_tombstone", AsyncMock()) as tombstone, \
             patch.object(engine, "persist_upload_state", AsyncMock()) as persist:
            response = self.client.post(
                "/batch/jobs/delete",
                params={
                    "job_name": "jobs/generation-1",
                    "upload_id": "upload /1",
                    "expected_generation": 1,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["local_state_updated"], False)
        self.assertEqual(state["gemini_batch"]["status"], "waiting_for_rows")
        self.assertEqual(state["gemini_batch"]["generation"], 2)
        tombstone.assert_not_awaited()
        persist.assert_not_awaited()

    def test_same_generation_pre_persist_delete_still_records_tombstone(self):
        state = batch_state()
        state["gemini_batch"] = {
            "status": "waiting_for_rows",
            "generation": 2,
            "job_name": None,
            "error": None,
        }

        async def fake_tombstone(*_args, **_kwargs):
            return {
                "deleted_by_user_at": "now",
                "job_names": ["jobs/generation-2"],
                "top_level_deleted": False,
                "generation": 2,
            }

        with patch.object(engine, "_gemini_batch_delete_sync", return_value={"ok": True}), \
             patch.object(engine, "read_upload_artifact", AsyncMock(return_value=state)), \
             patch.object(engine, "_write_batch_deletion_tombstone", side_effect=fake_tombstone), \
             patch.object(engine, "persist_upload_state", AsyncMock()) as persist:
            response = self.client.post(
                "/batch/jobs/delete",
                params={
                    "job_name": "jobs/generation-2",
                    "upload_id": "upload /1",
                    "expected_generation": 2,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["local_state_updated"], True)
        self.assertIn("jobs/generation-2", state["batch_deleted_job_names"])
        persist.assert_awaited_once()

    def test_destructive_actions_invalidate_remote_job_cache(self):
        for action in ("cancel", "delete"):
            with self.subTest(action=action):
                engine.gemini_batch_list_cache = [{"name": "jobs/stale"}]
                engine.gemini_batch_list_cache_fetched_at = 123.0
                engine.gemini_batch_list_error_cooldown_until = 456.0
                response, _persist = self._post(
                    action, "jobs/top", copy.deepcopy(batch_state()))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(engine.gemini_batch_list_cache, [])
                self.assertEqual(engine.gemini_batch_list_cache_fetched_at, 0.0)
                self.assertEqual(engine.gemini_batch_list_error_cooldown_until, 0.0)


class TestBatchJobActionRealPersistence(unittest.IsolatedAsyncioTestCase):
    async def _sync_with_real_persist(self, action):
        state = batch_state()
        write_artifact = AsyncMock()
        finalize = AsyncMock()
        notify_terminal = Mock()
        run_batch = AsyncMock()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(engine, "UPLOAD_BASE_DIR", Path(tmp)), \
             patch.object(engine, "read_upload_artifact", AsyncMock(return_value=state)), \
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

    async def test_delete_chunk_with_terminal_sibling_does_not_relaunch_batch(self):
        upload_id = "terminal-sibling-upload"
        state = batch_state()
        state["upload_id"] = upload_id
        state["gemini_batch"]["chunks"][1]["status"] = "succeeded"
        run_batch = AsyncMock()

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(engine, "UPLOAD_BASE_DIR", Path(tmp)), \
             patch.dict("os.environ", {"S3_BUCKET": ""}, clear=False), \
             patch.object(engine, "read_upload_artifact", AsyncMock(return_value=state)), \
             patch.object(engine, "write_upload_artifact", AsyncMock()), \
             patch.object(engine, "update_summary_cache"), \
             patch.object(engine, "build_upload_output_payload", return_value={}), \
             patch.object(engine, "_batch_postprocess_enabled_for", return_value=True), \
             patch.object(engine, "_finalize_serpwow_outputs", AsyncMock()), \
             patch.object(engine, "_notify_slack_terminal"), \
             patch.object(engine, "run_gemini_batch_for_upload", run_batch):
            updated = await engine._sync_batch_job_action_local_state(
                upload_id, "jobs/chunk 0", "delete")
            await asyncio.sleep(0)

        task = engine.gemini_batch_tasks.pop(upload_id, None)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(updated)
        self.assertEqual(state["gemini_batch"]["status"], "completed_with_errors")
        run_batch.assert_not_awaited()

    async def test_delete_chunk_when_all_chunks_fail_does_not_relaunch_batch(self):
        upload_id = "all-terminal-failure-upload"
        state = batch_state()
        state["upload_id"] = upload_id
        state["gemini_batch"]["chunks"][1]["status"] = "failed"
        run_batch = AsyncMock()

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(engine, "UPLOAD_BASE_DIR", Path(tmp)), \
             patch.dict("os.environ", {"S3_BUCKET": ""}, clear=False), \
             patch.object(engine, "read_upload_artifact", AsyncMock(return_value=state)), \
             patch.object(engine, "write_upload_artifact", AsyncMock()), \
             patch.object(engine, "update_summary_cache"), \
             patch.object(engine, "build_upload_output_payload", return_value={}), \
             patch.object(engine, "_batch_postprocess_enabled_for", return_value=True), \
             patch.object(engine, "_finalize_serpwow_outputs", AsyncMock()), \
             patch.object(engine, "_notify_slack_terminal"), \
             patch.object(engine, "run_gemini_batch_for_upload", run_batch):
            updated = await engine._sync_batch_job_action_local_state(
                upload_id, "jobs/chunk 0", "delete")
            await asyncio.sleep(0)

        task = engine.gemini_batch_tasks.pop(upload_id, None)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(updated)
        self.assertEqual(state["gemini_batch"]["status"], "failed")
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
                 patch.object(engine, "_clear_batch_deletion_tombstone", AsyncMock()), \
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

    async def test_explicit_retry_waits_for_chunk_deleted_generation_to_exit(self):
        state = batch_state()
        state["rows"][0]["status"] = "failed"
        state["batch_deleted_job_names"] = ["jobs/chunk 0"]
        state["gemini_batch"]["deleted_jobs_by_user_at"] = "deleted-at"
        old_driver_exited = asyncio.Event()

        async def old_driver():
            try:
                await asyncio.Event().wait()
            finally:
                old_driver_exited.set()

        old_task = asyncio.create_task(old_driver())
        await asyncio.sleep(0)
        engine.gemini_batch_tasks["upload-1"] = old_task
        try:
            with patch.object(engine, "rabbitmq_exchange", MagicMock()), \
                 patch.object(engine, "get_upload_state", AsyncMock(return_value=state)), \
                 patch.object(engine, "read_upload_artifact", AsyncMock(return_value=state)), \
                 patch.object(engine, "_clear_batch_deletion_tombstone", AsyncMock()), \
                 patch.object(engine, "persist_upload_state", AsyncMock()), \
                 patch.object(engine, "publish_job", AsyncMock()), \
                 patch.object(engine, "_batch_postprocess_enabled_for", return_value=True):
                await engine.retry_failed_rows("upload-1", limit=0)
            self.assertTrue(old_driver_exited.is_set())
            self.assertTrue(old_task.cancelled())
        finally:
            engine.gemini_batch_tasks.pop("upload-1", None)
            if not old_task.done():
                old_task.cancel()
                await asyncio.gather(old_task, return_exceptions=True)

    async def test_stale_worker_snapshot_cannot_erase_separate_delete_tombstone(self):
        upload_id = "cross-process-upload"
        live_state = batch_state()
        live_state["upload_id"] = upload_id
        stale_worker_snapshot = copy.deepcopy(live_state)
        delayed_write_snapshot = copy.deepcopy(live_state)
        run_batch = AsyncMock()

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(engine, "UPLOAD_BASE_DIR", Path(tmp)), \
             patch.dict("os.environ", {"S3_BUCKET": ""}, clear=False), \
             patch.object(engine, "_batch_postprocess_enabled_for", return_value=True), \
             patch.object(engine, "_notify_slack_terminal"), \
             patch.object(engine, "_finalize_serpwow_outputs", AsyncMock()), \
             patch.object(engine, "run_gemini_batch_for_upload", run_batch):
            engine._write_json(
                engine._upload_dir(upload_id) / "state.json", live_state)
            updated = await engine._sync_batch_job_action_local_state(
                upload_id, "jobs/top", "delete")
            self.assertTrue(updated)

            # Independent worker had already read before delete and now writes
            # its stale completed snapshot after the API persisted deletion.
            await engine.persist_upload_state(upload_id, stale_worker_snapshot)
            await asyncio.sleep(0)
            # Model a delayed state.json/S3 writer that had already passed its
            # persist-time tombstone check before the delete committed.
            engine._write_json(engine._state_file(upload_id), delayed_write_snapshot)
            final_state = await engine.read_upload_artifact(upload_id, "state")

        task = engine.gemini_batch_tasks.pop(upload_id, None)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(final_state["gemini_batch"]["status"], "failed")
        self.assertTrue(final_state.get("batch_deleted_by_user_at"))
        run_batch.assert_not_awaited()

    async def test_s3_clear_marker_supersedes_another_instances_cached_tombstone(self):
        upload_id = "cross-instance-retry"
        state = batch_state()
        state["upload_id"] = upload_id
        remote_artifacts = {}

        def capture_s3_write(key, payload):
            remote_artifacts[key] = copy.deepcopy(payload)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(engine, "UPLOAD_BASE_DIR", Path(tmp)), \
             patch.dict("os.environ", {"S3_BUCKET": "bucket"}, clear=False), \
             patch.object(engine, "_write_json_to_s3_sync", side_effect=capture_s3_write), \
             patch.object(engine, "_read_json_from_s3_sync", side_effect=lambda key: copy.deepcopy(remote_artifacts[key])):
            await engine._write_batch_deletion_tombstone(
                upload_id, state, "jobs/chunk 0", "deleted-at",
                top_level_deleted=False,
            )
            await engine._clear_batch_deletion_tombstone(upload_id, state)

            # A second instance still has the pre-retry deletion cached.
            engine._write_json(
                engine._batch_deletion_tombstone_path(upload_id),
                {
                    "deleted_by_user_at": "deleted-at",
                    "job_names": ["jobs/chunk 0"],
                    "top_level_deleted": False,
                },
            )
            resolved = await engine._read_batch_deletion_tombstone(upload_id, state)

        self.assertTrue(resolved.get("cleared_at"))
        self.assertFalse(resolved.get("deleted_by_user_at"))
        stale_state = batch_state()
        stale_state["gemini_batch"]["status"] = "running"
        engine._apply_batch_deletion_tombstone(stale_state, resolved)
        self.assertEqual(stale_state["gemini_batch"]["generation"], resolved["generation"])
        self.assertEqual(stale_state["gemini_batch"]["status"], "waiting_for_rows")

    async def test_newer_local_retry_marker_beats_stale_s3_deletion(self):
        upload_id = "newer-local-retry"
        state = batch_state()
        state["upload_id"] = upload_id
        state["gemini_batch"]["generation"] = 2
        local_marker = {
            "upload_id": upload_id,
            "cleared_at": "2026-07-12T12:00:00+00:00",
            "generation": 2,
        }
        stale_remote_marker = {
            "upload_id": upload_id,
            "deleted_by_user_at": "2026-07-12T12:01:00+00:00",
            "job_names": ["jobs/top"],
            "top_level_deleted": True,
            "generation": 1,
        }

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(engine, "UPLOAD_BASE_DIR", Path(tmp)), \
             patch.dict("os.environ", {"S3_BUCKET": "bucket"}, clear=False), \
             patch.object(engine, "_read_json_from_s3_sync", return_value=stale_remote_marker):
            marker_path = engine._batch_deletion_tombstone_path(upload_id)
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            engine._write_json(marker_path, local_marker)
            resolved = await engine._read_batch_deletion_tombstone(upload_id, state)

        self.assertEqual(resolved, local_marker)

    def test_generation_one_deletion_cannot_override_generation_two_retry(self):
        state = batch_state()
        state["gemini_batch"] = {
            "status": "waiting_for_rows",
            "generation": 2,
            "job_name": None,
            "error": None,
        }
        tombstone = {
            "deleted_by_user_at": "2026-07-12T12:01:00+00:00",
            "job_names": ["jobs/top"],
            "top_level_deleted": True,
            "generation": 1,
        }

        engine._apply_batch_deletion_tombstone(state, tombstone)

        self.assertEqual(state["gemini_batch"]["status"], "waiting_for_rows")
        self.assertEqual(state["gemini_batch"]["generation"], 2)
        self.assertNotIn("batch_deleted_by_user_at", state)

    async def test_same_generation_newer_local_marker_beats_older_s3_marker(self):
        upload_id = "same-generation-recency"
        state = batch_state()
        state["upload_id"] = upload_id
        local_marker = {
            "upload_id": upload_id,
            "deleted_by_user_at": "2026-07-12T12:02:00+00:00",
            "job_names": ["jobs/top"],
            "top_level_deleted": True,
            "generation": 3,
        }
        older_remote_marker = {
            "upload_id": upload_id,
            "cleared_at": "2026-07-12T12:01:00+00:00",
            "generation": 3,
        }

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(engine, "UPLOAD_BASE_DIR", Path(tmp)), \
             patch.dict("os.environ", {"S3_BUCKET": "bucket"}, clear=False), \
             patch.object(engine, "_read_json_from_s3_sync", return_value=older_remote_marker):
            marker_path = engine._batch_deletion_tombstone_path(upload_id)
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            engine._write_json(marker_path, local_marker)
            resolved = await engine._read_batch_deletion_tombstone(upload_id, state)

        self.assertEqual(resolved, local_marker)

    async def test_old_process_generation_cannot_apply_results_after_retry(self):
        upload_id = "cross-process-generation"
        original = batch_state()
        original["upload_id"] = upload_id
        original["gemini_batch"]["status"] = "queued"
        original["gemini_batch"]["generation"] = 0
        persisted = {"state": original}

        async def fake_persist(_upload_id, latest):
            persisted["state"] = latest

        async def finish_old_generation(*_args):
            retried = copy.deepcopy(persisted["state"])
            retried["gemini_batch"] = {
                "status": "waiting_for_rows",
                "generation": 1,
                "job_name": None,
                "error": None,
            }
            persisted["state"] = retried
            return {
                "chunk_id": 0,
                "job_name": "jobs/old-generation",
                "status": "succeeded",
                "error": None,
                "parsed_by_row": {1: {"official_website": "https://stale.example"}},
                "usage": {},
            }

        with patch.object(
            engine, "read_upload_artifact",
            AsyncMock(side_effect=lambda _uid, _name: persisted["state"]),
        ), patch.object(
            engine, "persist_upload_state", side_effect=fake_persist,
        ), patch.object(
            engine, "_run_one_gemini_chunk", side_effect=finish_old_generation,
        ):
            await engine.run_gemini_batch_for_upload(upload_id)

        self.assertEqual(persisted["state"]["gemini_batch"]["generation"], 1)
        self.assertEqual(persisted["state"]["gemini_batch"]["status"], "waiting_for_rows")
        self.assertEqual(
            persisted["state"]["rows"][0]["result"]["official_website"],
            "https://acme.example",
        )

    async def test_waiting_retry_generation_cannot_be_adopted_by_old_driver(self):
        state = batch_state()
        state["gemini_batch"] = {
            "status": "waiting_for_rows",
            "generation": 1,
            "job_name": None,
            "error": None,
        }
        persist = AsyncMock()
        run_chunk = AsyncMock()

        with patch.object(
            engine, "read_upload_artifact", AsyncMock(return_value=state),
        ), patch.object(
            engine, "persist_upload_state", persist,
        ), patch.object(
            engine, "_run_one_gemini_chunk", run_chunk,
        ):
            await engine.run_gemini_batch_for_upload("upload /1")

        persist.assert_not_awaited()
        run_chunk.assert_not_awaited()
        self.assertEqual(state["gemini_batch"]["status"], "waiting_for_rows")

    async def test_maybe_start_preserves_retry_generation_when_queueing(self):
        upload_id = "versioned-retry"
        state = batch_state()
        state["status"] = "completed"
        state["gemini_batch"] = {
            "status": "waiting_for_rows",
            "generation": 2,
            "job_name": None,
            "error": None,
        }
        run_batch = AsyncMock()

        # The batch gate reads LLM_BATCH, which tests/__init__ blanks so the suite cannot
        # inherit a developer .env. State the intent here.
        with patch.dict(os.environ, {"LLM_BATCH": "true"}), \
             patch.object(engine, "persist_upload_state", AsyncMock()), \
             patch.object(engine, "run_gemini_batch_for_upload", run_batch):
            await engine.maybe_start_gemini_batch_for_upload(upload_id, state)
            await asyncio.sleep(0)

        task = engine.gemini_batch_tasks.pop(upload_id, None)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(state["gemini_batch"]["status"], "queued")
        self.assertEqual(state["gemini_batch"]["generation"], 2)
        run_batch.assert_awaited_once_with(upload_id)


if __name__ == "__main__":
    unittest.main()
