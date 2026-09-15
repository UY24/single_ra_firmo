# backend/tests/test_ai_mode_worker.py
"""AI Mode broker worker (PR2): idempotent scrape-job processing.

Offline: ScrapeDoClient faked; no broker needed (process_scrape_job is invoked
directly, as the consumer loop would after decoding a message).
"""
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
    "AI_MODE_STATUS_FLUSH_SEC": "0",   # flush status.json on every update
    "S3_BUCKET": "",
    "SLACK_WEBHOOK_URL": "",
}

RAW_PAYLOAD = {"text_blocks": [{"snippet": "notes"}], "references": []}
CSV_SIX = "company_name,country\n" + "".join(f"Company {i},Japan\n" for i in range(1, 7))


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


def _payload(run_id, idx, entities, *, batches_total=2, mode="ai_deep",
             company="Acme Corp", ptype="scrape"):
    return {
        "type": ptype, "run_id": run_id, "request_index": idx,
        "batches_total": batches_total, "mode": mode, "company_name": company,
        "entities": [asdict(e) for e in entities],
    }


class WorkerHarness(unittest.TestCase):
    def setUp(self):
        FakeScrapeDoClient.calls = 0
        FailingScrapeDoClient.calls = 0
        self._tmp = tempfile.TemporaryDirectory()
        results_root = Path(self._tmp.name) / "ai_mode_results"
        self._patches = [
            mock.patch.dict(os.environ, FAKE_ENV),
            mock.patch.object(run_store, "AI_MODE_RESULTS_DIR", results_root),
            mock.patch.object(ai_mode_service, "ScrapeDoClient", FakeScrapeDoClient),
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
        self.entities = [Entity(company_name=f"Company {i}", country="Japan", sno=i)
                         for i in range(1, 4)]

    def tearDown(self):
        ai_worker._reset_for_tests()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _status(self):
        return json.loads((self.run_dir / "status.json").read_text(encoding="utf-8"))


class TestProcessScrapeJob(WorkerHarness):
    def test_fresh_job_writes_raw_file_and_counts(self):
        asyncio.run(ai_worker.process_scrape_job(
            _payload(self.run_id, 1, self.entities)))
        self.assertTrue(
            (self.run_dir / "raw_responses" / "request_000001.json").exists())
        self.assertEqual(FakeScrapeDoClient.calls, 1)
        status = self._status()
        self.assertEqual(status["batches_done"], 1)
        self.assertEqual(status["scrapedo_failed_requests"], 0)
        log = (self.run_dir / "run.log").read_text(encoding="utf-8")
        self.assertIn("scrape batch 1 started", log)
        self.assertIn("scrape batch 1 finished status=success", log)
        self.assertIn("started_at=", log)
        self.assertIn("finished_at=", log)

    def test_existing_raw_file_is_not_rescraped(self):
        raw = self.run_dir / "raw_responses" / "request_000001.json"
        raw.write_text(json.dumps(RAW_PAYLOAD), encoding="utf-8")
        asyncio.run(ai_worker.process_scrape_job(
            _payload(self.run_id, 1, self.entities)))
        self.assertEqual(FakeScrapeDoClient.calls, 0)

    def test_scrape_failure_writes_error_marker_and_counts(self):
        with mock.patch.object(ai_mode_service, "ScrapeDoClient", FailingScrapeDoClient):
            asyncio.run(ai_worker.process_scrape_job(
                _payload(self.run_id, 2, self.entities)))
        marker = self.run_dir / "raw_responses" / "request_000002.error.json"
        self.assertTrue(marker.exists())
        record = json.loads(marker.read_text(encoding="utf-8"))
        self.assertIn("429", record["error"])
        status = self._status()
        self.assertEqual(status["scrapedo_failed_requests"], 1)
        self.assertEqual(status["entities_without_scrape_data"], 3)

    def test_existing_error_marker_skips_scrape(self):
        marker = self.run_dir / "raw_responses" / "request_000002.error.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"error": "boom"}), encoding="utf-8")
        asyncio.run(ai_worker.process_scrape_job(
            _payload(self.run_id, 2, self.entities)))
        self.assertEqual(FakeScrapeDoClient.calls, 0)

    def test_unknown_run_is_dropped_without_error(self):
        payload = _payload("nonexistent-run", 1, self.entities, company="")
        asyncio.run(ai_worker.process_scrape_job(payload))  # must not raise
        self.assertEqual(FakeScrapeDoClient.calls, 0)


class FakeLLMClient:
    """Sync-path LLM returning a JSON array covering the batch's snos."""

    def complete_json(self, messages):
        from app.services.ai_mode.models import TokenUsage

        text = messages[-1]["content"]
        arr = []
        for sno in range(1, 7):
            if f"Company {sno}" in text:
                arr.append({
                    "sno": sno, "company_name": f"Company {sno}", "country": "Japan",
                    "website_url": f"https://c{sno}.example.com" if sno % 2 else None,
                    "confidence": 70, "flags": [], "attempt_log": [],
                })
        return arr, TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)


class TestRunFinish(WorkerHarness):
    """run_ai_mode_finish: Phases 2+3 standalone, rebuilding Phase-1 state from
    disk (raw files + error markers) — the broker worker's finish path."""

    def test_finish_from_disk_completes_run(self):
        raw_dir = self.run_dir / "raw_responses"
        # Batch 1 scraped OK; batch 2 terminally failed (error marker).
        (raw_dir / "request_000001.json").write_text(
            json.dumps(RAW_PAYLOAD), encoding="utf-8")
        (raw_dir / "request_000002.error.json").write_text(
            json.dumps({"request_index": 2, "error": "HTTP 429", "seconds": 1.5}),
            encoding="utf-8")
        ai_mode_service.set_status_fields(self.run_id, phase="cleaning")

        with mock.patch.object(ai_mode_service, "make_llm_client",
                               lambda cfg: FakeLLMClient()):
            ai_mode_service.run_ai_mode_finish(self.run_id)

        status = self._status()
        self.assertEqual(status["status"], "completed_with_errors")
        report = json.loads((self.run_dir / "final_report.json").read_text(encoding="utf-8"))
        # 6 entities: 3 from the cleaned batch, 3 error rows from the failed scrape.
        self.assertEqual(len(report["entities"]), 6)
        self.assertEqual(report["summary"]["outcome_breakdown"]["errored"], 3)
        self.assertEqual(report["summary"]["outcome_breakdown"]["found"], 2)  # snos 1,3
        requests = {r["request_index"]: r for r in report["requests"]}
        self.assertEqual(requests[2]["status"], "error")
        self.assertIn("429", requests[2]["error"])
        self.assertTrue((self.run_dir / "cleaned" / "batch-000001.json").exists())
        self.assertTrue((self.run_dir / "found.csv").exists())

    def test_finish_never_raises_and_marks_failed_on_crash(self):
        ai_mode_service.set_status_fields(self.run_id, phase="cleaning")
        with mock.patch.object(ai_mode_service, "make_llm_client",
                               side_effect=RuntimeError("no key")):
            ai_mode_service.run_ai_mode_finish(self.run_id)  # must not raise
        self.assertEqual(self._status()["status"], "failed")


class TestPublishRunBatches(WorkerHarness):
    """Producer side: one scrape message per batch + a trailing check."""

    def _capture(self):
        published = []

        async def fake_pub(payload):
            published.append(payload)

        return published, mock.patch.object(
            ai_worker.broker, "publish_scrape_job", side_effect=fake_pub)

    def test_publishes_all_batches_with_schema_then_check(self):
        published, patcher = self._capture()
        with patcher:
            count = asyncio.run(ai_worker.publish_run_batches(self.run_id))
        self.assertEqual(count, 2)
        self.assertEqual(len(published), 3)  # 2 scrape + 1 check
        first, second, check = published
        self.assertEqual(first["type"], "scrape")
        self.assertEqual(first["request_index"], 1)
        self.assertEqual(first["batches_total"], 2)
        self.assertEqual(first["mode"], "ai_deep")
        self.assertEqual(first["company_name"], "Acme Corp")
        self.assertEqual(len(first["entities"]), 3)
        self.assertEqual(first["entities"][0]["company_name"], "Company 1")
        self.assertEqual(second["request_index"], 2)
        self.assertEqual(check["type"], "check")
        self.assertEqual(check["run_id"], self.run_id)
        status = self._status()
        self.assertEqual(status["engine"], "broker")
        self.assertEqual(status["phase"], "publishing")
        self.assertEqual(status["batches_total"], 2)
        self.assertEqual(status["status"], "running")
        self.assertTrue(status.get("started_at"))

    def test_only_missing_skips_batches_with_raw_or_error_files(self):
        raw_dir = self.run_dir / "raw_responses"
        (raw_dir / "request_000001.json").write_text(
            json.dumps(RAW_PAYLOAD), encoding="utf-8")
        published, patcher = self._capture()
        with patcher:
            count = asyncio.run(
                ai_worker.publish_run_batches(self.run_id, only_missing=True))
        self.assertEqual(count, 1)
        scrapes = [p for p in published if p["type"] == "scrape"]
        self.assertEqual([p["request_index"] for p in scrapes], [2])
        self.assertEqual(published[-1]["type"], "check")


class TestPublishCrashMarksRunFailed(WorkerHarness):
    """A crashed background publish must surface as status=failed, never vanish
    (the router fire-and-forgets the task; the old engine's never-raises
    contract lives inside publish_run_batches now)."""

    def test_publish_crash_sets_failed_status_and_error(self):
        (self.run_dir / "input.csv").unlink()  # _load_groups will raise
        count = asyncio.run(ai_worker.publish_run_batches(self.run_id))  # must not raise
        self.assertEqual(count, 0)
        status = self._status()
        self.assertEqual(status["status"], "failed")
        self.assertIn("publish", (status.get("error") or "").lower())


class TestResumeClearsCorruptCleaned(WorkerHarness):
    def test_reset_deletes_cleaned_file_with_unparseable_text(self):
        cleaned_dir = self.run_dir / "cleaned"
        cleaned_dir.mkdir(exist_ok=True)
        good = cleaned_dir / "batch-000001.json"
        good.write_text(json.dumps({"key": "batch-000001",
                                    "text": json.dumps([{"sno": 1}]), "usage": None}),
                        encoding="utf-8")
        corrupt = cleaned_dir / "batch-000002.json"
        corrupt.write_text(json.dumps({"key": "batch-000002",
                                       "text": "I could not find any websites, sorry!",
                                       "usage": None}),
                           encoding="utf-8")
        ai_worker.reset_run_for_resume(self.run_id, self.run_dir)
        # The parseable checkpoint is kept (no LLM re-spend); the garbage one is
        # cleared so the finish task re-cleans that batch instead of erroring it
        # forever on every resume.
        self.assertTrue(good.exists())
        self.assertFalse(corrupt.exists())


class TestCountDoneBatches(WorkerHarness):
    def test_counts_raw_and_error_files_separately(self):
        raw_dir = self.run_dir / "raw_responses"
        (raw_dir / "request_000001.json").write_text("{}", encoding="utf-8")
        (raw_dir / "request_000002.error.json").write_text("{}", encoding="utf-8")
        raw, err = ai_worker.count_done_batches(self.run_dir)
        self.assertEqual((raw, err), (1, 1))

    def test_same_index_with_raw_and_error_marker_counts_once(self):
        # A stale error marker alongside a later-landed raw file must not
        # double-count and satisfy the barrier while another batch is missing.
        raw_dir = self.run_dir / "raw_responses"
        (raw_dir / "request_000001.json").write_text("{}", encoding="utf-8")
        (raw_dir / "request_000001.error.json").write_text("{}", encoding="utf-8")
        raw, err = ai_worker.count_done_batches(self.run_dir)
        self.assertEqual((raw, err), (1, 0))


class TestCorruptRawResume(WorkerHarness):
    def test_resume_republishes_batch_with_unparseable_raw_file(self):
        # Crash mid-write leaves a truncated raw file: resume must retry it,
        # not skip it (existence alone is not "scraped").
        raw_dir = self.run_dir / "raw_responses"
        (raw_dir / "request_000001.json").write_text(
            json.dumps(RAW_PAYLOAD), encoding="utf-8")
        (raw_dir / "request_000002.json").write_text("{truncat", encoding="utf-8")
        published = []

        async def fake_pub(payload):
            published.append(payload)

        with mock.patch.object(ai_worker.broker, "publish_scrape_job",
                               side_effect=fake_pub):
            count = asyncio.run(
                ai_worker.publish_run_batches(self.run_id, only_missing=True))
        self.assertEqual(count, 1)
        scrapes = [p for p in published if p["type"] == "scrape"]
        self.assertEqual([p["request_index"] for p in scrapes], [2])


class TestInfraFailureRetryBudget(WorkerHarness):
    """Restart-safe poison guard: retries tracked in status.json, not via the
    broker's redelivered flag (which graceful shutdown also sets)."""

    def test_first_failure_requeues_then_budget_exhausts(self):
        payload = _payload(self.run_id, 1, self.entities)
        message = mock.Mock(redelivered=True)  # e.g. requeued by a restart
        self.assertTrue(asyncio.run(
            ai_worker._should_requeue_infra_failure(payload, message)))
        self.assertEqual(self._status()["delivery_failures"], {"1": 1})
        self.assertFalse(asyncio.run(
            ai_worker._should_requeue_infra_failure(payload, message)))

    def test_undecodable_payload_falls_back_to_redelivered_flag(self):
        self.assertTrue(asyncio.run(ai_worker._should_requeue_infra_failure(
            None, mock.Mock(redelivered=False))))
        self.assertFalse(asyncio.run(ai_worker._should_requeue_infra_failure(
            None, mock.Mock(redelivered=True))))


if __name__ == "__main__":
    unittest.main()
