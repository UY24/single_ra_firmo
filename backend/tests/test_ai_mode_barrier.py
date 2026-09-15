# backend/tests/test_ai_mode_barrier.py
"""Phase 1 -> 2 barrier (PR2): the last scraped batch flips the run to cleaning
and dispatches run_ai_mode_finish exactly once (task-registry guarded)."""
import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from app.models.entities import Entity
from app.services.ai_mode import ai_mode_service, run_store
from app.services.ai_mode import worker as ai_worker

FAKE_ENV = {
    "SCRAPEDO_TOKEN": "fake-token",
    "GEMINI_API_KEY": "fake-key",
    "LLM_BATCH": "",           # sync-LLM cleanup path (offline)
    "AI_MODE_STATUS_FLUSH_SEC": "0",
    "S3_BUCKET": "",
    "SLACK_WEBHOOK_URL": "",
}

RAW_PAYLOAD = {"text_blocks": [{"snippet": "notes"}], "references": []}
CSV_SIX = "company_name,country\n" + "".join(f"Company {i},Japan\n" for i in range(1, 7))


class FakeScrapeDoClient:
    def __init__(self, **kwargs):
        pass

    def search_google_ai_mode(self, query, extra_params=None):
        return RAW_PAYLOAD


class TestBarrier(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        results_root = Path(self._tmp.name) / "ai_mode_results"
        self.finish_calls = []
        self._patches = [
            mock.patch.dict(os.environ, FAKE_ENV),
            mock.patch.object(run_store, "AI_MODE_RESULTS_DIR", results_root),
            mock.patch.object(ai_mode_service, "ScrapeDoClient", FakeScrapeDoClient),
            mock.patch.object(ai_mode_service, "run_ai_mode_finish",
                              side_effect=lambda run_id: self.finish_calls.append(run_id),
                              create=True),
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
        ai_mode_service.set_status_fields(
            self.run_id, phase="scraping", engine="broker", batches_total=2,
            status="running",
        )
        self.groups = [
            [Entity(company_name=f"Company {i}", country="Japan", sno=i) for i in (1, 2, 3)],
            [Entity(company_name=f"Company {i}", country="Japan", sno=i) for i in (4, 5, 6)],
        ]

    def tearDown(self):
        ai_worker._reset_for_tests()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _payload(self, idx, *, ptype="scrape"):
        return {
            "type": ptype, "run_id": self.run_id, "request_index": idx,
            "batches_total": 2, "mode": "ai_deep", "company_name": "Acme Corp",
            "entities": [asdict(e) for e in self.groups[idx - 1]] if ptype == "scrape" else [],
        }

    def _status(self):
        return json.loads((self.run_dir / "status.json").read_text(encoding="utf-8"))

    def test_last_batch_flips_phase_and_dispatches_finish_once(self):
        async def run():
            await ai_worker.process_scrape_job(self._payload(1))
            self.assertEqual(self._status()["phase"], "scraping")
            self.assertEqual(self.finish_calls, [])
            await ai_worker.process_scrape_job(self._payload(2))
            await asyncio.gather(*list(ai_worker._finish_tasks.values()))
        asyncio.run(run())
        self.assertEqual(self._status()["phase"], "cleaning")
        self.assertEqual(self.finish_calls, [self.run_id])

    def test_concurrent_completion_checks_dispatch_only_one_finish(self):
        raw_dir = self.run_dir / "raw_responses"
        for i in (1, 2):
            (raw_dir / f"request_{i:06d}.json").write_text(
                json.dumps(RAW_PAYLOAD), encoding="utf-8")

        async def run():
            await asyncio.gather(
                ai_worker._completion_check(self.run_id, self.run_dir),
                ai_worker._completion_check(self.run_id, self.run_dir),
            )
            await asyncio.gather(*list(ai_worker._finish_tasks.values()))
        asyncio.run(run())
        self.assertEqual(self.finish_calls, [self.run_id])

    def test_check_message_triggers_completion(self):
        raw_dir = self.run_dir / "raw_responses"
        (raw_dir / "request_000001.json").write_text(json.dumps(RAW_PAYLOAD), encoding="utf-8")
        (raw_dir / "request_000002.error.json").write_text(
            json.dumps({"error": "boom"}), encoding="utf-8")

        async def run():
            await ai_worker.process_scrape_job(self._payload(0, ptype="check") | {"request_index": 0})
            await asyncio.gather(*list(ai_worker._finish_tasks.values()))
        asyncio.run(run())
        status = self._status()
        self.assertEqual(status["phase"], "cleaning")
        self.assertEqual(status["batches_done"], 2)
        self.assertEqual(status["scrapedo_failed_requests"], 1)
        self.assertEqual(self.finish_calls, [self.run_id])

    def test_incomplete_files_do_not_flip(self):
        raw_dir = self.run_dir / "raw_responses"
        (raw_dir / "request_000001.json").write_text(json.dumps(RAW_PAYLOAD), encoding="utf-8")

        async def run():
            await ai_worker._completion_check(self.run_id, self.run_dir)
        asyncio.run(run())
        self.assertEqual(self._status()["phase"], "scraping")
        self.assertEqual(self.finish_calls, [])


if __name__ == "__main__":
    unittest.main()
