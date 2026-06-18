# backend/tests/test_ai_mode_resume.py
"""Offline test: Phase-2 resume reuses cleaned batches and never re-scrapes.

Drives the Gemini-batch cleanup path (AI_MODE_LLM_BATCH=true) with the
gemini_batch seams mocked. Pre-seeds raw_responses/ (so Phase 1 resumes with no
scrape.do calls) and one cleaned/ batch (so Phase 2 re-submits only the rest).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.ai_mode import ai_mode_service, run_store

# Two ai_deep batches of 3 → request_001 (snos 1-3), request_002 (snos 4-6).
CSV_SIX = "company_name,country\n" + "".join(f"Company {i},Japan\n" for i in range(1, 7))

FAKE_ENV = {
    "SCRAPEDO_TOKEN": "fake-token",
    "GEMINI_API_KEY": "fake-key",
    "AI_MODE_LLM_BATCH": "1",          # force the Gemini-batch cleanup path
    "AI_MODE_LLM_PROVIDER": "gemini",
    "SCRAPEDO_CONCURRENCY": "2",
    "S3_BUCKET": "",                    # keep the S3 mirror a no-op (offline)
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
        for env in ("AI_BULK_BATCH_SIZE", "AI_DEEP_BATCH_SIZE", "SCRAPEDO_BATCH_SIZE", "AI_MODE_BATCH_POLL_SEC"):
            os.environ.pop(env, None)
        os.environ["AI_MODE_BATCH_POLL_SEC"] = "5"  # min; no sleep happens (single poll, all terminal)
        self.results_root = results_root

    def tearDown(self):
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

        # Pre-seed BOTH raw files (Phase 1 reuses → no scrape.do) and ONE cleaned
        # batch (Phase 2 should skip batch-000001, submit only batch-000002).
        raw_dir = run_dir / ai_mode_service.RAW_RESPONSES_DIRNAME
        raw_dir.mkdir(parents=True, exist_ok=True)
        for i in (1, 2):
            (raw_dir / f"request_{i:03d}.json").write_text(json.dumps(RAW_PAYLOAD), encoding="utf-8")
        cleaned_dir = run_dir / ai_mode_service.CLEANED_DIRNAME
        cleaned_dir.mkdir(parents=True, exist_ok=True)
        (cleaned_dir / "batch-000001.json").write_text(
            json.dumps({"key": "batch-000001",
                        "text": json.dumps(_full_array()),
                        "usage": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}),
            encoding="utf-8",
        )

        ai_mode_service.run_ai_mode_sync(run_id, resume=True)

        # No scrape.do calls — raw files were reused.
        self.assertEqual(FakeScrapeDoClient.queries, [])
        # Only the un-cleaned batch was submitted to Gemini.
        self.assertNotIn("batch-000001", self.fake_gemini.submitted)
        self.assertIn("batch-000002", self.fake_gemini.submitted)

        status = ai_mode_service.get_ai_mode_status(run_id)
        self.assertIn(status["status"], ("completed", "completed_with_errors"))
        report = json.loads((run_dir / "final_report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["entities"]), 6)
        # Both batches contributed (odd snos found across the full run).
        self.assertEqual(report["summary"]["websites_found"], 3)
        # A cleaned file now exists for the batch that was (re)submitted this run.
        self.assertTrue((cleaned_dir / "batch-000002.json").exists())


if __name__ == "__main__":
    unittest.main()
