"""
Regression tests for maybe_reconcile_gemini_batch_status — chunked-run guard.

C1: chunked runs must NOT be force-failed by the single-job stale guard even
when started_at is older than GEMINI_BATCH_STARTUP_TIMEOUT_SEC (180 s).
"""
import asyncio
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

from app.services.serpwow import engine as app


def _chunked_running_state(age_seconds: int = 300) -> dict:
    """Return a state dict that looks like a live chunked run."""
    started_at = (
        datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    ).isoformat()
    return {
        "upload_id": "u-chunked-test",
        "pipeline": "gsearch",
        "gemini_batch": {
            "status": "running",
            "started_at": started_at,
            # No top-level job_name — chunked shape
            "chunks": [
                {"chunk_id": 0, "job_name": "batches/chunk-0", "status": "running", "error": None},
                {"chunk_id": 1, "job_name": "batches/chunk-1", "status": "queued", "error": None},
            ],
        },
    }


class TestChunkedStatusReconcileGuard(unittest.IsolatedAsyncioTestCase):
    def test_remote_cancelled_states_are_terminal(self):
        for live_state in ("JOB_STATE_CANCELLED", "BATCH_STATE_CANCELLED"):
            with self.subTest(live_state=live_state):
                self.assertEqual(app._derive_ui_batch_status(
                    live_state=live_state,
                    done_flag=True,
                    error_obj=None,
                    local_status="cancel_requested",
                ), "cancelled")

    async def test_cancel_requested_reconciles_once_to_cancelled(self):
        state = {
            "upload_id": "u-cancelled",
            "pipeline": "gsearch",
            "status": "completed",
            "gemini_batch": {"status": "cancel_requested", "job_name": "batches/cancelled"},
        }
        persisted = []

        async def fake_persist(_uid, latest):
            persisted.append(latest)

        get_batch = mock.Mock(return_value={
            "done": True,
            "state": {"name": "JOB_STATE_CANCELLED"},
        })
        with mock.patch.dict("os.environ", {"LLM_BATCH": "true"}, clear=False), \
             mock.patch.object(app, "_gemini_batch_get_sync", get_batch), \
             mock.patch.object(app, "read_upload_artifact",
                               new=mock.AsyncMock(return_value=state)), \
             mock.patch.object(app, "persist_upload_state", new=fake_persist):
            result = await app.maybe_reconcile_gemini_batch_status("u-cancelled", state)
            self.assertEqual(result["gemini_batch"]["status"], "cancelled")
            self.assertTrue(result["gemini_batch"].get("completed_at"))
            self.assertIsNone(result["gemini_batch"].get("error"))
            self.assertFalse(app._batch_postprocess_pending(result))
            get_batch.reset_mock()
            again = await app.maybe_reconcile_gemini_batch_status("u-cancelled", result)

            restart_persist = mock.AsyncMock()
            with mock.patch.object(app, "persist_upload_state", new=restart_persist):
                await app.maybe_start_gemini_batch_for_upload("u-cancelled", result)

        self.assertEqual(again["gemini_batch"]["status"], "cancelled")
        get_batch.assert_not_called()
        self.assertEqual(len(persisted), 1)
        restart_persist.assert_not_called()

    async def test_chunked_running_not_force_failed_after_startup_timeout(self):
        """C1 regression: a chunked run older than GEMINI_BATCH_STARTUP_TIMEOUT_SEC
        must NOT be force-failed by maybe_reconcile_gemini_batch_status."""
        state = _chunked_running_state(age_seconds=300)  # 300s > 180s timeout

        persist_mock = mock.AsyncMock()
        with mock.patch.dict(
            "os.environ",
            {"LLM_BATCH": "true", "GEMINI_API_KEY": "k"},
            clear=False,
        ), mock.patch.object(
            app, "read_upload_artifact", new=mock.AsyncMock(return_value=state)
        ), mock.patch.object(
            app, "persist_upload_state", new=persist_mock
        ):
            result = await app.maybe_reconcile_gemini_batch_status("u-chunked-test", state)

        # Status must still be running — NOT flipped to failed
        self.assertEqual(
            result["gemini_batch"]["status"],
            "running",
            "C1: chunked run was incorrectly force-failed by the single-job stale guard",
        )
        # persist_upload_state must NOT have been called (no mutation)
        persist_mock.assert_not_called()

    async def test_legacy_single_job_still_handled(self):
        """Non-chunked shape (no chunks key) must pass through to existing logic."""
        # A legacy state with status=queued and a job_name — should return unchanged
        # because local_status is not in the stale-guard branch (no started_at).
        state = {
            "upload_id": "u-legacy",
            "pipeline": "gsearch",
            "gemini_batch": {
                "status": "queued",
                # No chunks list
            },
        }
        with mock.patch.dict(
            "os.environ",
            {"LLM_BATCH": "true", "GEMINI_API_KEY": "k"},
            clear=False,
        ):
            result = await app.maybe_reconcile_gemini_batch_status("u-legacy", state)
        # No job_name → falls through to the `if not job_name: return state` guard → unchanged
        self.assertEqual(result["gemini_batch"]["status"], "queued")


class TestChunkedCancelEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_calls_per_chunk_not_400(self):
        """I1: cancelling a chunked run must cancel per-chunk jobs, not 400."""
        state = {
            "upload_id": "u-cancel-test",
            "pipeline": "gsearch",
            "gemini_batch": {
                "status": "running",
                "chunks": [
                    {"chunk_id": 0, "job_name": "batches/c0", "status": "running", "error": None},
                    {"chunk_id": 1, "job_name": "batches/c1", "status": "running", "error": None},
                ],
            },
        }

        cancelled = []

        def fake_cancel(job_name):
            cancelled.append(job_name)
            return {}

        with mock.patch.object(
            app, "get_upload_state", new=mock.AsyncMock(return_value=state)
        ), mock.patch.object(
            app, "read_upload_artifact", new=mock.AsyncMock(return_value=state)
        ), mock.patch.object(
            app, "persist_upload_state", new=mock.AsyncMock()
        ), mock.patch.object(
            app, "_gemini_batch_cancel_sync", side_effect=fake_cancel
        ):
            from fastapi.testclient import TestClient
            client = TestClient(app.app)
            response = client.post("/batch/jobs/u-cancel-test/cancel")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "cancel_requested")
        # Both chunks cancelled
        self.assertIn("batches/c0", cancelled)
        self.assertIn("batches/c1", cancelled)


if __name__ == "__main__":
    unittest.main()
