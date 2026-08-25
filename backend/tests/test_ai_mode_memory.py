# backend/tests/test_ai_mode_memory.py
"""PR1 memory rework: scrape records carry no payload; assembly streams from disk.

`scrape_batch_sync` is the extracted, idempotent one-batch scraper (also the
worker's unit of work in the broker engine). It must never return the scrape.do
payload — Phase 2/3 re-read raw_responses/ from disk per batch so a 1M-row run
holds at most one batch in memory.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.models.entities import Entity
from app.services.ai_mode import ai_mode_service, run_store
from app.services.ai_mode.mode_config import get_mode

FAKE_ENV = {
    "SCRAPEDO_TOKEN": "fake-token",
    "GEMINI_API_KEY": "fake-key",
    "LLM_BATCH": "",           # sync-LLM cleanup path
    "SCRAPEDO_CONCURRENCY": "2",
    "S3_BUCKET": "",
    "SLACK_WEBHOOK_URL": "",
}

RAW_PAYLOAD = {"text_blocks": [{"snippet": "notes"}], "references": []}


def _entities(n):
    return [Entity(company_name=f"Company {i}", country="Japan", sno=i)
            for i in range(1, n + 1)]


class FakeScrapeDoClient:
    calls = 0

    def __init__(self, **kwargs):
        pass

    def search_google_ai_mode(self, query, extra_params=None):
        type(self).calls += 1
        return RAW_PAYLOAD


class FailingScrapeDoClient(FakeScrapeDoClient):
    def search_google_ai_mode(self, query, extra_params=None):
        type(self).calls += 1
        raise RuntimeError("HTTP 429 Too Many Requests")


class TestScrapeBatchSync(unittest.TestCase):
    def setUp(self):
        FakeScrapeDoClient.calls = 0
        FailingScrapeDoClient.calls = 0
        self._env = mock.patch.dict(os.environ, FAKE_ENV)
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name) / "acme-corp" / "run1"
        (self.run_dir / "raw_responses").mkdir(parents=True)
        self.mode = get_mode("ai_deep")
        self.settings = ai_mode_service.build_ai_mode_settings()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_writes_raw_file_and_returns_metadata_without_payload(self):
        rec = ai_mode_service.scrape_batch_sync(
            self.run_dir, self.mode, self.settings, FakeScrapeDoClient(),
            request_index=1, group=_entities(3),
        )
        self.assertTrue(rec["ok"])
        self.assertIsNone(rec["error"])
        self.assertNotIn("payload", rec)
        self.assertEqual(rec["rel_raw_path"], "raw_responses/request_000001.json")
        raw = json.loads((self.run_dir / "raw_responses" / "request_000001.json").read_text())
        self.assertEqual(raw, RAW_PAYLOAD)
        self.assertEqual(FakeScrapeDoClient.calls, 1)

    def test_existing_parseable_raw_is_reused_without_scraping(self):
        (self.run_dir / "raw_responses" / "request_000002.json").write_text(
            json.dumps(RAW_PAYLOAD), encoding="utf-8"
        )
        rec = ai_mode_service.scrape_batch_sync(
            self.run_dir, self.mode, self.settings, FakeScrapeDoClient(),
            request_index=2, group=_entities(3),
        )
        self.assertTrue(rec["ok"])
        self.assertNotIn("payload", rec)
        self.assertEqual(FakeScrapeDoClient.calls, 0)

    def test_scrape_failure_returns_error_record(self):
        rec = ai_mode_service.scrape_batch_sync(
            self.run_dir, self.mode, self.settings, FailingScrapeDoClient(),
            request_index=3, group=_entities(3),
        )
        self.assertFalse(rec["ok"])
        self.assertIn("429", rec["error"])
        self.assertIsNone(rec["rel_raw_path"])
        self.assertFalse((self.run_dir / "raw_responses" / "request_000003.json").exists())


class FakeLLMClient:
    """Sync-path LLM: returns a JSON array covering the batch's snos."""

    def __init__(self, groups_by_call=None):
        self.calls = 0

    def complete_json(self, messages):
        self.calls += 1
        # The user prompt embeds the entity list; recover snos from it.
        text = messages[-1]["content"]
        arr = []
        for sno in range(1, 7):
            if f"Company {sno}" in text:
                arr.append({
                    "sno": sno, "company_name": f"Company {sno}", "country": "Japan",
                    "website_url": f"https://c{sno}.example.com" if sno % 2 else None,
                    "confidence": 70, "flags": [], "attempt_log": [],
                })
        from app.services.ai_mode.models import TokenUsage
        return arr, TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)


class TestBrokerRunStreamsFromDisk(unittest.TestCase):
    """End-to-end broker-driven run: outputs correct after the streaming rework."""

    def setUp(self):
        from app.services.ai_mode import worker as ai_worker

        FakeScrapeDoClient.calls = 0
        self._tmp = tempfile.TemporaryDirectory()
        results_root = Path(self._tmp.name) / "ai_mode_results"
        self.llm = FakeLLMClient()
        self._patches = [
            mock.patch.dict(os.environ, FAKE_ENV),
            mock.patch.object(run_store, "AI_MODE_RESULTS_DIR", results_root),
            mock.patch.object(ai_mode_service, "ScrapeDoClient", FakeScrapeDoClient),
            mock.patch.object(ai_mode_service, "make_llm_client", lambda cfg: self.llm),
        ]
        for p in self._patches:
            p.start()
        for env in ("AI_BULK_BATCH_SIZE", "AI_DEEP_BATCH_SIZE"):
            os.environ.pop(env, None)
        ai_worker._reset_for_tests()

    def tearDown(self):
        from app.services.ai_mode import worker as ai_worker

        ai_worker._reset_for_tests()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_run_produces_streamed_outputs(self):
        from tests.ai_mode_drive import drive_run

        csv_six = "company_name,country\n" + "".join(
            f"Company {i},Japan\n" for i in range(1, 7)
        )
        info = ai_mode_service.prepare_ai_mode_run(
            csv_six.encode("utf-8"), "input.csv",
            mode_key="ai_deep", company_name="Acme Corp", company_id="acme-1",
        )
        run_id = info["run_id"]
        drive_run(run_id)

        run_dir = run_store.find_run_dir(run_id)
        status = ai_mode_service.get_ai_mode_status(run_id)
        self.assertEqual(status["status"], "completed")
        report = json.loads((run_dir / "final_report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["entities"]), 6)
        self.assertEqual(report["summary"]["websites_found"], 3)
        self.assertEqual(report["summary"]["websites_not_found"], 3)
        self.assertEqual(
            report["summary"]["outcome_breakdown"],
            {"found": 3, "not_found": 3, "errored": 0},
        )
        # Both scrape batches hit scrape.do once each; cleaned files persisted.
        self.assertEqual(FakeScrapeDoClient.calls, 2)
        self.assertTrue((run_dir / "cleaned" / "batch-000001.json").exists())
        self.assertTrue((run_dir / "cleaned" / "batch-000002.json").exists())
        found_csv = (run_dir / "found.csv").read_text(encoding="utf-8")
        self.assertIn("https://c1.example.com", found_csv)


if __name__ == "__main__":
    unittest.main()
