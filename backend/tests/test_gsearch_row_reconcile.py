import asyncio
import unittest
from unittest import mock

from app.services.serpwow import engine as app


def _row(idx, status, age_sec):
    from datetime import datetime, timezone, timedelta
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_sec)).isoformat()
    return {"row_index": idx, "company_name": f"C{idx}", "country": "US",
            "status": status, "status_updated_at": ts, "requeue_attempts": 0}


class TestRowReconcile(unittest.IsolatedAsyncioTestCase):
    async def _run(self, state, max_requeue="1"):
        published = []
        persisted = {}

        async def fake_publish(job): published.append(job)

        async def fake_persist(uid, st): persisted["state"] = st

        with mock.patch.dict("os.environ", {
            "GSEARCH_ROW_STALE_TIMEOUT_SEC": "60", "GSEARCH_ROW_MAX_REQUEUE": max_requeue,
        }, clear=False), \
             mock.patch.object(app, "_list_local_state_files_sync", return_value=[]), \
             mock.patch.object(app, "_collect_states_for_reconcile", new=mock.AsyncMock(return_value=[state])), \
             mock.patch.object(app, "_get_rabbitmq_queue_depth", new=mock.AsyncMock(return_value=0)), \
             mock.patch.object(app, "read_upload_artifact", new=mock.AsyncMock(return_value=state)), \
             mock.patch.object(app, "persist_upload_state", new=fake_persist), \
             mock.patch.object(app, "publish_job", new=fake_publish), \
             mock.patch.object(app, "rabbitmq_exchange", object()), \
             mock.patch.object(app, "rabbitmq_queue", object()):
            await app.reconcile_stuck_gsearch_rows()
        return published, persisted.get("state")

    async def test_stale_row_first_requeued(self):
        state = {"upload_id": "u1", "pipeline": "gsearch", "status": "processing",
                 "rows": [_row(1, "completed", 0), _row(2, "processing", 9999)]}
        published, _ = await self._run(state)
        self.assertEqual([j["row_index"] for j in published], [2])

    async def test_row_force_failed_after_max_requeue(self):
        r = _row(2, "queued", 9999); r["requeue_attempts"] = 1
        state = {"upload_id": "u1", "pipeline": "gsearch", "status": "processing",
                 "rows": [_row(1, "completed", 0), r]}
        published, new_state = await self._run(state)
        self.assertEqual(published, [])
        failed = [x for x in new_state["rows"] if x["row_index"] == 2][0]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("terminalized", (failed.get("error") or ""))

    async def test_the_reconciler_ignores_gmaps_entirely(self):
        """gmaps has no per-row messages since it moved to the S3-only runner — nothing
        to re-publish, and its own stale-run scan (gmaps_runner.redrive_stale_runs) is
        what recovers it. Re-publishing here would push a row job no consumer reads."""
        state = {"upload_id": "u1", "pipeline": "gmaps", "status": "processing",
                 "rows": [_row(1, "completed", 0), _row(2, "processing", 9999)]}
        published_list = []

        async def fake_publish(job): published_list.append(job)

        with mock.patch.dict("os.environ", {
            "GSEARCH_ROW_STALE_TIMEOUT_SEC": "60", "GSEARCH_ROW_MAX_REQUEUE": "1",
        }, clear=False), \
             mock.patch.object(app, "_list_local_state_files_sync", return_value=[]), \
             mock.patch.object(app, "_collect_states_for_reconcile", new=mock.AsyncMock(return_value=[state])), \
             mock.patch.object(app, "_get_rabbitmq_queue_depth", new=mock.AsyncMock(return_value=0)), \
             mock.patch.object(app, "read_upload_artifact", new=mock.AsyncMock(return_value=state)), \
             mock.patch.object(app, "publish_job", new=fake_publish), \
             mock.patch.object(app, "rabbitmq_exchange", object()), \
             mock.patch.object(app, "rabbitmq_queue", object()):
            await app.reconcile_stuck_gsearch_rows()

        self.assertEqual(published_list, [])
    async def test_gmaps_heuristic_mode_is_skipped(self):
        # Heuristic gmaps (default mode) has no Gemini-batch barrier -> the
        # reconciler must NOT touch it, even with a long-stuck row.
        state = {"upload_id": "u1", "pipeline": "gmaps", "status": "processing",
                 "rows": [_row(1, "completed", 0), _row(2, "processing", 9999)]}
        published, new_state = await self._run(state)
        self.assertEqual(published, [])
        self.assertIsNone(new_state)

    async def test_gmaps_per_row_llm_mode_is_skipped(self):
        # LLM confidence mode but GMAPS_LLM_BATCH unset/false -> per-row LLM calls
        # happen inside the worker itself; still no batch barrier -> skip.
        state = {"upload_id": "u1", "pipeline": "gmaps", "status": "processing",
                 "rows": [_row(1, "completed", 0), _row(2, "processing", 9999)]}
        published = []
        persisted = {}

        async def fake_publish(job): published.append(job)

        async def fake_persist(uid, st): persisted["state"] = st

        with mock.patch.dict("os.environ", {
            "GSEARCH_ROW_STALE_TIMEOUT_SEC": "60", "GSEARCH_ROW_MAX_REQUEUE": "1",
            "GMAPS_CONFIDENCE_MODE": "llm", "GMAPS_LLM_BATCH": "false",
        }, clear=False), \
             mock.patch.object(app, "_list_local_state_files_sync", return_value=[]), \
             mock.patch.object(app, "_collect_states_for_reconcile", new=mock.AsyncMock(return_value=[state])), \
             mock.patch.object(app, "_get_rabbitmq_queue_depth", new=mock.AsyncMock(return_value=0)), \
             mock.patch.object(app, "read_upload_artifact", new=mock.AsyncMock(return_value=state)), \
             mock.patch.object(app, "persist_upload_state", new=fake_persist), \
             mock.patch.object(app, "publish_job", new=fake_publish), \
             mock.patch.object(app, "rabbitmq_exchange", object()), \
             mock.patch.object(app, "rabbitmq_queue", object()):
            await app.reconcile_stuck_gsearch_rows()

        self.assertEqual(published, [])
        self.assertEqual(persisted, {})


if __name__ == "__main__":
    unittest.main()
