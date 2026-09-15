# backend/tests/test_ai_mode_resume.py
"""Offline test: broker resume reuses cleaned batches and never re-scrapes.

Drives the Gemini-batch cleanup path (LLM_BATCH=true) with the
gemini_batch seams mocked. Pre-seeds raw_responses/ (so the resume republish
skips every batch -> no scrape.do calls) and one cleaned/ batch (so the finish
task re-submits only the rest to Gemini).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.ai_mode import ai_mode_service, run_store
from app.services.ai_mode import worker as ai_worker
from tests.ai_mode_drive import drive_run

# Two ai_deep batches of 3 → request_000001 (snos 1-3), request_000002 (snos 4-6).
CSV_SIX = "company_name,country\n" + "".join(f"Company {i},Japan\n" for i in range(1, 7))

FAKE_ENV = {
    "SCRAPEDO_TOKEN": "fake-token",
    "GEMINI_API_KEY": "fake-key",
    "LLM_BATCH": "1",          # force the Gemini-batch cleanup path
    "SCRAPEDO_CONCURRENCY": "2",
    "S3_BUCKET": "",                    # keep the S3 mirror a no-op (offline)
    "SLACK_WEBHOOK_URL": "",            # never post to a real Slack webhook from tests
}

RAW_PAYLOAD = {"text_blocks": [{"snippet": "research notes covering all companies"}], "references": []}


def _full_array() -> list[dict]:
    """A cleanup array covering all six snos (parse_cleanup_response maps by sno)."""
    out = []
    for s in range(1, 7):
        out.append({
            "sno": s, "company_name": f"Company {s}", "country": "Japan",
            "website_url": f"https://company{s}.example.com" if s % 2 else None,
            "confidence": 80 if s % 2 else 10, "flags": [], "attempt_log": [],
        })
    return out


class FakeScrapeDoClient:
    queries: list[str] = []

    def __init__(self, **kwargs):
        pass

    def search_google_ai_mode(self, query, extra_params=None):
        type(self).queries.append(query)
        return RAW_PAYLOAD


class FakeGemini:
    """Captures which keys are submitted; returns a success for each shard."""

    def __init__(self):
        self.submitted: list[str] = []
        self._keys_by_name: dict[str, list[str]] = {}

    def create_batch(self, model, shard, *, display_name):
        keys = [k for k, _ in shard]
        self.submitted.extend(keys)
        name = f"batches/{len(self._keys_by_name) + 1}"
        self._keys_by_name[name] = keys
        return {"name": name}

    def get_batch(self, name):
        return {"done": True, "state": {"name": "JOB_STATE_SUCCEEDED"}, "_keys": self._keys_by_name.get(name, [])}

    def collect_results(self, batch_obj):
        usage = {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}
        return [
            {"key": k, "text": json.dumps(_full_array()), "usage": usage, "error": None}
            for k in batch_obj.get("_keys", [])
        ]


class TestPhase2Resume(unittest.TestCase):
    def setUp(self):
        FakeScrapeDoClient.queries = []
        self.fake_gemini = FakeGemini()
        self._tmp = tempfile.TemporaryDirectory()
        results_root = Path(self._tmp.name) / "ai_mode_results"
        gb = ai_mode_service.gemini_batch
        self._patches = [
            mock.patch.dict(os.environ, FAKE_ENV),
            mock.patch.object(run_store, "AI_MODE_RESULTS_DIR", results_root),
            mock.patch.object(ai_mode_service, "ScrapeDoClient", FakeScrapeDoClient),
            mock.patch.object(ai_mode_service, "make_llm_client", lambda cfg: object()),
            mock.patch.object(gb, "create_batch", self.fake_gemini.create_batch),
            mock.patch.object(gb, "get_batch", self.fake_gemini.get_batch),
            mock.patch.object(gb, "collect_results", self.fake_gemini.collect_results),
        ]
        for p in self._patches:
            p.start()
        for env in ("AI_BULK_BATCH_SIZE", "AI_DEEP_BATCH_SIZE", "AI_MODE_BATCH_POLL_SEC"):
            os.environ.pop(env, None)
        os.environ["AI_MODE_BATCH_POLL_SEC"] = "5"  # min; no sleep happens (single poll, all terminal)
        ai_worker._reset_for_tests()
        self.results_root = results_root

    def tearDown(self):
        ai_worker._reset_for_tests()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_resume_skips_cleaned_batch_and_does_not_rescrape(self):
        info = ai_mode_service.prepare_ai_mode_run(
            CSV_SIX.encode("utf-8"), "input.csv",
            mode_key="ai_deep", company_name="Acme Corp", company_id="acme-1",
        )
        run_id = info["run_id"]
        run_dir = run_store.find_run_dir(run_id)
        self.assertIsNotNone(run_dir)

        # Pre-seed BOTH raw files (the resume republish skips them → no
        # scrape.do), a stale error marker (reset must clear it), and ONE cleaned
        # batch (the finish task should skip batch-000001, submit only -000002).
        raw_dir = run_dir / ai_mode_service.RAW_RESPONSES_DIRNAME
        raw_dir.mkdir(parents=True, exist_ok=True)
        for i in (1, 2):
            (raw_dir / f"request_{i:06d}.json").write_text(json.dumps(RAW_PAYLOAD), encoding="utf-8")
        stale_marker = raw_dir / "request_000003.error.json"
        stale_marker.write_text(json.dumps({"error": "old failure"}), encoding="utf-8")
        cleaned_dir = run_dir / ai_mode_service.CLEANED_DIRNAME
        cleaned_dir.mkdir(parents=True, exist_ok=True)
        (cleaned_dir / "batch-000001.json").write_text(
            json.dumps({"key": "batch-000001",
                        "text": json.dumps(_full_array()),
                        "usage": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}),
            encoding="utf-8",
        )
        # Mimic the crashed run the resume endpoint acts on.
        ai_mode_service.set_status_fields(
            run_id, status="failed", engine="broker",
            gemini_batch_jobs=["batches/stale"], requeue_attempts={"2": 1},
        )

        # The endpoint's two actions: reset, then republish only missing batches.
        cleared = ai_worker.reset_run_for_resume(run_id, run_dir)
        self.assertEqual(cleared, 1)
        self.assertFalse(stale_marker.exists())
        published = drive_run(run_id, only_missing=True)

        # Both raws existed → only the check message was published; no scrapes.
        self.assertEqual([p["type"] for p in published], ["check"])
        self.assertEqual(FakeScrapeDoClient.queries, [])
        # Stale bookkeeping cleared by the reset.
        status = ai_mode_service.get_ai_mode_status(run_id)
        self.assertNotIn("batches/stale", status.get("gemini_batch_jobs") or [])
        # Only the un-cleaned batch was submitted to Gemini.
        self.assertNotIn("batch-000001", self.fake_gemini.submitted)
        self.assertIn("batch-000002", self.fake_gemini.submitted)

        self.assertIn(status["status"], ("completed", "completed_with_errors"))
        report = json.loads((run_dir / "final_report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["entities"]), 6)
        # Both batches contributed (odd snos found across the full run).
        self.assertEqual(report["summary"]["websites_found"], 3)
        # A cleaned file now exists for the batch that was (re)submitted this run.
        self.assertTrue((cleaned_dir / "batch-000002.json").exists())


class TestResumeEndpoint(unittest.TestCase):
    """POST /uploads/ai-mode/{run_id}/resume — broker gates + dispatch contract."""

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routers.ai_mode import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_resume_503_when_broker_down(self):
        res = self._client().post("/uploads/ai-mode/whatever/resume")
        self.assertEqual(res.status_code, 503)
        self.assertIn("queue", res.json()["detail"].lower())

    def test_resume_resets_and_republishes_only_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            results_root = Path(tmp) / "ai_mode_results"
            publish = mock.AsyncMock(return_value=0)
            with mock.patch.dict(os.environ, FAKE_ENV), \
                    mock.patch.object(run_store, "AI_MODE_RESULTS_DIR", results_root), \
                    mock.patch("app.services.ai_mode.broker.is_ready", return_value=True), \
                    mock.patch("app.services.ai_mode.worker.publish_run_batches", publish):
                info = ai_mode_service.prepare_ai_mode_run(
                    CSV_SIX.encode("utf-8"), "input.csv",
                    mode_key="ai_deep", company_name="Acme Corp", company_id="acme-1",
                )
                run_id = info["run_id"]
                run_dir = run_store.find_run_dir(run_id)
                marker = run_dir / "raw_responses" / "request_000002.error.json"
                marker.write_text(json.dumps({"error": "boom"}), encoding="utf-8")

                # Non-terminal status → 409, nothing dispatched.
                ai_mode_service.set_status_fields(run_id, status="running")
                res = self._client().post(f"/uploads/ai-mode/{run_id}/resume")
                self.assertEqual(res.status_code, 409)
                publish.assert_not_called()
                self.assertTrue(marker.exists())

                # Failed status → markers cleared + republish(only_missing=True).
                ai_mode_service.set_status_fields(run_id, status="failed")
                res = self._client().post(f"/uploads/ai-mode/{run_id}/resume")
                self.assertEqual(res.status_code, 200)
                body = res.json()
                self.assertTrue(body["resumed"])
                self.assertEqual(body["cleared_error_markers"], 1)
                self.assertFalse(marker.exists())
                publish.assert_called_once_with(run_id, only_missing=True)


class TestS3Rehydrate(unittest.TestCase):
    def test_rehydrate_pulls_only_matching_run_to_local(self):
        from app.services.ai_mode import s3_sync

        run_id = "abc123def"
        keys = [
            f"acme-corp/ai_bulk/{run_id}/status.json",
            f"acme-corp/ai_bulk/{run_id}/raw_responses/request_000001.json",
            f"acme-corp/ai_bulk/{run_id}/cleaned/batch-000001.json",
            "other-co/ai_deep/zzz999/status.json",  # unrelated → must be ignored
        ]

        def fake_download(key, local_path):
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            Path(local_path).write_text("{}", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            results_root = Path(tmp) / "ai_mode_results"
            with mock.patch.object(s3_sync, "AI_MODE_RESULTS_DIR", results_root), \
                 mock.patch.object(s3_sync.s3, "is_configured", return_value=True), \
                 mock.patch.object(s3_sync.s3, "iter_keys", return_value=iter(keys)), \
                 mock.patch.object(s3_sync.s3, "download_file", side_effect=fake_download):
                out = s3_sync.rehydrate_run_from_s3(run_id)

            run_dir = results_root / "acme-corp" / run_id
            self.assertEqual(out, run_dir)
            self.assertTrue((run_dir / "status.json").exists())
            self.assertTrue((run_dir / "raw_responses" / "request_000001.json").exists())
            self.assertTrue((run_dir / "cleaned" / "batch-000001.json").exists())
            # the unrelated run's company folder must NOT be created
            self.assertFalse((results_root / "other-co").exists())

    def test_rehydrate_returns_none_when_unconfigured(self):
        from app.services.ai_mode import s3_sync
        with mock.patch.object(s3_sync.s3, "is_configured", return_value=False):
            self.assertIsNone(s3_sync.rehydrate_run_from_s3("whatever"))


class TestWriteThroughFile(unittest.TestCase):
    def test_mirror_file_uses_run_prefixed_key(self):
        from app.services.ai_mode import s3_sync
        captured = {}

        def fake_upload(local_path, key):
            captured["key"] = key

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "acme-corp" / "run9"  # <company>/<run_id>
            (run_dir / "raw_responses").mkdir(parents=True)
            f = run_dir / "raw_responses" / "request_000007.json"
            f.write_text("{}", encoding="utf-8")
            with mock.patch.object(s3_sync.s3, "is_configured", return_value=True), \
                 mock.patch.object(s3_sync.s3, "upload_file", side_effect=fake_upload):
                ok = s3_sync.mirror_file_to_s3(run_dir, "ai_bulk", f)
        self.assertTrue(ok)
        self.assertEqual(captured["key"],
                         "acme-corp/ai_bulk/run9/raw_responses/request_000007.json")

    def test_mirror_file_noop_when_unconfigured(self):
        from app.services.ai_mode import s3_sync
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "co" / "r"
            run_dir.mkdir(parents=True)
            f = run_dir / "x.txt"
            f.write_text("y", encoding="utf-8")
            with mock.patch.object(s3_sync.s3, "is_configured", return_value=False):
                self.assertFalse(s3_sync.mirror_file_to_s3(run_dir, "ai_bulk", f))

    def test_run_s3_uri(self):
        from app.services.ai_mode import s3_sync
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "acme-corp" / "run9"
            run_dir.mkdir(parents=True)
            with mock.patch.object(s3_sync.s3, "is_configured", return_value=True), \
                 mock.patch.object(s3_sync.s3, "bucket_name", return_value="website-url-finder"):
                self.assertEqual(
                    s3_sync.run_s3_uri(run_dir, "ai_bulk", "found.csv"),
                    "s3://website-url-finder/acme-corp/ai_bulk/run9/found.csv",
                )
            with mock.patch.object(s3_sync.s3, "is_configured", return_value=False):
                self.assertIsNone(s3_sync.run_s3_uri(run_dir, "ai_bulk", "found.csv"))


if __name__ == "__main__":
    unittest.main()
