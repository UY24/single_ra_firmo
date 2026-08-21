# backend/tests/test_ai_mode_reconcile.py
"""reconcile_ai_mode_runs (PR3): the always-on durability backstop.

Covers: republish of lost/stale batches (attempt-capped, then terminalized via
error markers so the barrier resolves), drained-queue gating, re-dispatch of a
dead finish task, and the legacy phantom-'running' flip that makes the existing
"Rerun failed" button reachable after a hard kill.
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.ai_mode import ai_mode_service, run_store
from app.services.ai_mode import worker as ai_worker

FAKE_ENV = {
    "SCRAPEDO_TOKEN": "fake-token",
    "GEMINI_API_KEY": "fake-key",
    "LLM_BATCH": "",
    "AI_MODE_STATUS_FLUSH_SEC": "0",
    "AI_MODE_BATCH_STALE_TIMEOUT_SEC": "900",
    "AI_MODE_BATCH_MAX_REQUEUE": "1",
    "S3_BUCKET": "",
    "SLACK_WEBHOOK_URL": "",
}

RAW_PAYLOAD = {"text_blocks": [{"snippet": "notes"}], "references": []}
CSV_SIX = "company_name,country\n" + "".join(f"Company {i},Japan\n" for i in range(1, 7))
STALE_TS = "2020-01-01T00:00:00+00:00"


class ReconcileHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        results_root = Path(self._tmp.name) / "ai_mode_results"
        self.published = []
        self.finish_calls = []

        async def fake_pub(payload):
            self.published.append(payload)

        self._patches = [
            mock.patch.dict(os.environ, FAKE_ENV),
            mock.patch.object(run_store, "AI_MODE_RESULTS_DIR", results_root),
            mock.patch.object(ai_worker.broker, "publish_scrape_job", side_effect=fake_pub),
            mock.patch.object(ai_worker.broker, "get_queue_depth",
                              mock.AsyncMock(return_value=0)),
            mock.patch.object(ai_mode_service, "run_ai_mode_finish",
                              side_effect=lambda run_id: self.finish_calls.append(run_id)),
        ]
        for p in self._patches:
            p.start()
        ai_worker._reset_for_tests()
        info = ai_mode_service.prepare_ai_mode_run(
            CSV_SIX.encode("utf-8"), "input.csv",
            mode_key="ai_deep", company_name="Acme Corp", company_id="acme-1",
        )
        self.run_id = info["run_id"]
        self.run_dir = run_store.find_run_dir(self.run_id)

    def tearDown(self):
        ai_worker._reset_for_tests()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _set_status(self, **fields):
        ai_mode_service.set_status_fields(self.run_id, **fields)

    def _status(self):
        return json.loads((self.run_dir / "status.json").read_text(encoding="utf-8"))

    def _reconcile(self):
        asyncio.run(ai_worker.reconcile_ai_mode_runs())

    def _scrapes(self):
        return [p for p in self.published if p["type"] == "scrape"]


class TestRepublishMissing(ReconcileHarness):
    def test_stale_missing_batches_republished_once_with_attempts(self):
        # Batch 1 scraped; batch 2 lost. Run stale (old updated_at, no newer files).
        raw = self.run_dir / "raw_responses" / "request_000001.json"
        raw.write_text(json.dumps(RAW_PAYLOAD), encoding="utf-8")
        old = 100.0  # epoch — far older than the stale timeout
        os.utime(raw, (old, old))
        self._set_status(status="running", engine="broker", phase="scraping",
                         batches_total=2, updated_at=STALE_TS)
        self._reconcile()
        scrapes = self._scrapes()
        self.assertEqual([p["request_index"] for p in scrapes], [2])
        self.assertEqual(len(scrapes[0]["entities"]), 3)
        self.assertEqual(self._status()["requeue_attempts"], {"2": 1})
        self.assertEqual(self.finish_calls, [])

    def test_exhausted_attempts_terminalize_and_dispatch_finish(self):
        raw = self.run_dir / "raw_responses" / "request_000001.json"
        raw.write_text(json.dumps(RAW_PAYLOAD), encoding="utf-8")
        old = 100.0
        os.utime(raw, (old, old))
        self._set_status(status="running", engine="broker", phase="scraping",
                         batches_total=2, updated_at=STALE_TS,
                         requeue_attempts={"2": 1})

        async def run():
            await ai_worker.reconcile_ai_mode_runs()
            await asyncio.gather(*list(ai_worker._finish_tasks.values()))
        asyncio.run(run())

        marker = self.run_dir / "raw_responses" / "request_000002.error.json"
        self.assertTrue(marker.exists())
        self.assertIn("reconciler", json.loads(marker.read_text())["error"])
        self.assertEqual(self._scrapes(), [])
        self.assertEqual(self.finish_calls, [self.run_id])
        self.assertEqual(self._status()["phase"], "cleaning")

    def test_queue_not_drained_is_a_noop(self):
        self._set_status(status="running", engine="broker", phase="scraping",
                         batches_total=2, updated_at=STALE_TS)
        with mock.patch.object(ai_worker.broker, "get_queue_depth",
                               mock.AsyncMock(return_value=5)):
            self._reconcile()
        self.assertEqual(self.published, [])
        self.assertNotIn("requeue_attempts", self._status())

    def test_fresh_run_is_untouched(self):
        from app.services.ai_mode.models import utc_now_iso
        self._set_status(status="running", engine="broker", phase="scraping",
                         batches_total=2, updated_at=utc_now_iso())
        self._reconcile()
        self.assertEqual(self.published, [])

    def test_complete_files_flip_and_dispatch_finish(self):
        raw_dir = self.run_dir / "raw_responses"
        for i in (1, 2):
            (raw_dir / f"request_{i:06d}.json").write_text(
                json.dumps(RAW_PAYLOAD), encoding="utf-8")
        self._set_status(status="running", engine="broker", phase="scraping",
                         batches_total=2, updated_at=STALE_TS)

        async def run():
            await ai_worker.reconcile_ai_mode_runs()
            await asyncio.gather(*list(ai_worker._finish_tasks.values()))
        asyncio.run(run())
        self.assertEqual(self.finish_calls, [self.run_id])
        self.assertEqual(self._status()["phase"], "cleaning")


class TestFinishRedispatch(ReconcileHarness):
    def test_cleaning_run_with_no_live_task_is_redispatched(self):
        self._set_status(status="running", engine="broker", phase="cleaning",
                         batches_total=2, updated_at=STALE_TS)

        async def run():
            await ai_worker.reconcile_ai_mode_runs()
            await asyncio.gather(*list(ai_worker._finish_tasks.values()))
        asyncio.run(run())
        self.assertEqual(self.finish_calls, [self.run_id])


class TestLegacyPhantomFlip(ReconcileHarness):
    def test_stale_legacy_running_run_is_marked_failed(self):
        # Legacy sync-engine run (no engine field) hard-killed mid-run.
        self._set_status(status="running", updated_at=STALE_TS)
        self._reconcile()
        status = self._status()
        self.assertEqual(status["status"], "failed")
        self.assertIn("interrupted", (status.get("error") or "").lower())
        self.assertEqual(self.finish_calls, [])

    def test_fresh_legacy_running_run_is_untouched(self):
        from app.services.ai_mode.models import utc_now_iso
        self._set_status(status="running", updated_at=utc_now_iso())
        self._reconcile()
        self.assertEqual(self._status()["status"], "running")


if __name__ == "__main__":
    unittest.main()
