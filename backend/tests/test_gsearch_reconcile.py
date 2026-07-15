"""Offline tests for the Gemini-batch durability reconciler."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.serpwow import engine as la


def _write_state(base: str, upload_id: str, pipeline: str, batch_status):
    p = Path(base) / upload_id / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    st = {"upload_id": upload_id, "pipeline": pipeline, "status": "completed"}
    if batch_status is not None:
        st["gemini_batch"] = {"status": batch_status}
    p.write_text(json.dumps(st), encoding="utf-8")
    return p


class ReconcileTests(unittest.TestCase):
    def test_resumes_running_batch_and_skips_others(self):
        with tempfile.TemporaryDirectory() as d:
            paths = [
                _write_state(d, "R1", "gsearch", "running"),    # resume
                _write_state(d, "Q1", "gsearch", "queued"),     # resume
                _write_state(d, "D1", "gsearch", "succeeded"),  # terminal -> skip
                _write_state(d, "N1", "gmaps", None),           # not a batch pipeline -> skip
            ]
            la.gemini_batch_tasks.clear()

            async def _run():
                with mock.patch.object(la, "_list_local_state_files_sync", return_value=paths), \
                        mock.patch.object(la, "_batch_postprocess_enabled_for",
                                          side_effect=lambda p: p == "gsearch"), \
                        mock.patch.object(la, "run_gemini_batch_for_upload",
                                          new=mock.AsyncMock(return_value=None)) as run_mock:
                    await la.reconcile_pending_gemini_batches()
                    dispatched = sorted(run_mock.call_args_list and
                                        [c.args[0] for c in run_mock.call_args_list])
                    # drain the created tasks so the loop closes cleanly
                    tasks = list(la.gemini_batch_tasks.values())
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    return dispatched

            dispatched = asyncio.run(_run())
            self.assertEqual(dispatched, ["Q1", "R1"])
            self.assertIn("R1", la.gemini_batch_tasks)
            self.assertIn("Q1", la.gemini_batch_tasks)
            self.assertNotIn("D1", la.gemini_batch_tasks)
            self.assertNotIn("N1", la.gemini_batch_tasks)
            la.gemini_batch_tasks.clear()

    def test_skips_already_tracked_upload(self):
        with tempfile.TemporaryDirectory() as d:
            paths = [_write_state(d, "R1", "gsearch", "running")]
            la.gemini_batch_tasks.clear()

            async def _run():
                # an in-flight (not-done) task already exists for R1 -> must not re-dispatch
                async def _never():
                    await asyncio.sleep(3600)
                existing = asyncio.create_task(_never())
                la.gemini_batch_tasks["R1"] = existing
                with mock.patch.object(la, "_list_local_state_files_sync", return_value=paths), \
                        mock.patch.object(la, "_batch_postprocess_enabled_for", return_value=True), \
                        mock.patch.object(la, "run_gemini_batch_for_upload",
                                          new=mock.AsyncMock()) as run_mock:
                    await la.reconcile_pending_gemini_batches()
                    called = run_mock.call_count
                existing.cancel()
                try:
                    await existing
                except asyncio.CancelledError:
                    pass
                return called

            self.assertEqual(asyncio.run(_run()), 0)
            la.gemini_batch_tasks.clear()


if __name__ == "__main__":
    unittest.main()
