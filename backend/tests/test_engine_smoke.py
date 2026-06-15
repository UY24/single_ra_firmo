# backend/tests/test_engine_smoke.py
"""Offline end-to-end smoke test for the unified two-mode AI engine.

Mocks the module seams (ScrapeDoClient + make_llm_client) so no network access
is needed, and proves mode-specific batching, the new on-disk layout, and the
unified output schema.
"""
import csv
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.ai_mode import ai_mode_service, run_store
from app.services.ai_mode.models import TokenUsage

CSV_SIX = "company_name,country\n" + "".join(f"Company {i},Japan\n" for i in range(1, 7))

FAKE_ENV = {
    "SCRAPEDO_TOKEN": "fake-token",
    "GEMINI_API_KEY": "fake-key",
    "AI_MODE_LLM_BATCH": "",
    "AI_MODE_LLM_PROVIDER": "gemini",
    "SCRAPEDO_CONCURRENCY": "2",
}


class FakeScrapeDoClient:
    """Stands in for ScrapeDoClient; records each search query."""

    queries: list[str] = []

    def __init__(self, **kwargs):
        pass

    def search_google_ai_mode(self, query, extra_params=None):
        type(self).queries.append(query)
        payload = {
            "text_blocks": [{"snippet": "research notes covering all listed companies"}],
            "references": [],
        }
        return payload


class FakeLLMClient:
    """Returns a canned cleanup JSON array covering exactly the batch's snos.

    Odd snos get a website, even snos do not (so found.csv and notFound.csv
    both get rows).
    """

    def complete_json(self, messages):
        user = messages[-1]["content"]
        entities_block = user.split("Raw search response:")[0]
        snos = [int(m) for m in re.findall(r"^(\d+)\.", entities_block, re.M)]
        arr = []
        for s in snos:
            arr.append({
                "sno": s,
                "company_name": f"Company {s}",
                "country": "Japan",
                "website_url": f"https://company{s}.example.com" if s % 2 else None,
                "confidence": 80 if s % 2 else 10,
                "flags": [{"flag": "name_match", "why": "matched"}],
                "attempt_log": [{"query": f"Company {s} Japan", "result": "checked", "url": None}],
            })
        return arr, TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)


class TestEngineSmoke(unittest.TestCase):
    def setUp(self):
        FakeScrapeDoClient.queries = []
        self._tmp = tempfile.TemporaryDirectory()
        results_root = Path(self._tmp.name) / "ai_mode_results"
        self._patches = [
            mock.patch.dict(os.environ, FAKE_ENV),
            mock.patch.object(run_store, "AI_MODE_RESULTS_DIR", results_root),
            mock.patch.object(ai_mode_service, "ScrapeDoClient", FakeScrapeDoClient),
            mock.patch.object(ai_mode_service, "make_llm_client", lambda cfg: FakeLLMClient()),
        ]
        for p in self._patches:
            p.start()
        for env in ("AI_BULK_BATCH_SIZE", "AI_DEEP_BATCH_SIZE", "SCRAPEDO_BATCH_SIZE"):
            os.environ.pop(env, None)
        self.results_root = results_root

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _run(self, mode_key):
        info = ai_mode_service.prepare_ai_mode_run(
            CSV_SIX.encode("utf-8"), "input.csv",
            mode_key=mode_key, company_name="Acme Corp", company_id="acme-id-1",
        )
        ai_mode_service.run_ai_mode_sync(info["run_id"])
        return info

    def test_ai_bulk_six_entities_one_scrape_call(self):
        info = self._run("ai_bulk")
        self.assertEqual(info["batch_size"], 10)
        self.assertEqual(len(FakeScrapeDoClient.queries), 1)
        # bulk prompt text made it into the search query, with entities substituted
        self.assertIn("Company 3", FakeScrapeDoClient.queries[0])
        self.assertNotIn("{entities}", FakeScrapeDoClient.queries[0])

    def test_ai_deep_six_entities_two_scrape_calls(self):
        info = self._run("ai_deep")
        self.assertEqual(info["batch_size"], 3)
        self.assertEqual(len(FakeScrapeDoClient.queries), 2)
        self.assertIn("OSINT", FakeScrapeDoClient.queries[0])

    def test_outputs_layout_and_schema(self):
        info = self._run("ai_bulk")
        run_id = info["run_id"]
        run_dir = self.results_root / "acme-corp" / run_id
        self.assertTrue(run_dir.is_dir())

        for name in ("input.csv", "found.csv", "notFound.csv", "final_report.json",
                     "run.log", "status.json"):
            self.assertTrue((run_dir / name).exists(), name)
        self.assertTrue((run_dir / "raw_responses" / "request_001.json").exists())
        self.assertFalse((run_dir / "report.json").exists())
        self.assertFalse((run_dir / "ai_mode_debug.log").exists())

        with (run_dir / "found.csv").open(newline="", encoding="utf-8") as fh:
            found_rows = list(csv.DictReader(fh))
        with (run_dir / "notFound.csv").open(newline="", encoding="utf-8") as fh:
            notfound_reader = csv.DictReader(fh)
            notfound_fields = notfound_reader.fieldnames
            notfound_rows = list(notfound_reader)
        for col in ("company_name", "country", "website_url", "confidence", "flags", "attempt_log"):
            self.assertIn(col, found_rows[0])
        self.assertIn("error", notfound_fields)
        self.assertEqual(len(found_rows), 3)       # odd snos 1, 3, 5
        self.assertEqual(len(notfound_rows), 3)    # even snos 2, 4, 6
        self.assertEqual(found_rows[0]["website_url"], "https://company1.example.com")
        self.assertIn("name_match", found_rows[0]["flags"])
        self.assertIn("Company 1 Japan", found_rows[0]["attempt_log"])

        report = json.loads((run_dir / "final_report.json").read_text(encoding="utf-8"))
        self.assertEqual(set(report), {"summary", "requests", "entities"})
        self.assertEqual(report["summary"]["mode"], "ai_bulk")
        self.assertEqual(report["summary"]["websites_found"], 3)
        self.assertEqual(report["summary"]["websites_not_found"], 3)
        self.assertEqual(report["summary"]["token_usage"]["total_tokens"], 150)
        self.assertEqual(len(report["requests"]), 1)
        self.assertEqual(report["requests"][0]["status"], "success")
        self.assertEqual(report["requests"][0]["raw_json_file"], "raw_responses/request_001.json")
        self.assertNotIn("request_cost", report["requests"][0])
        self.assertEqual(len(report["entities"]), 6)

        # cost summary: llm tokens priced; scrape.do is flat-fee → just a search count
        cost = report["summary"]["cost"]
        self.assertEqual(cost["scrapedo_searches"], 1)
        self.assertNotIn("scrapedo_usd", cost)
        self.assertGreater(cost["llm_usd"], 0.0)  # 100/50 tokens at default Gemini rates
        self.assertEqual(cost["total_usd"], cost["llm_usd"])

        status = ai_mode_service.get_ai_mode_status(run_id)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["cost"], cost)
        self.assertEqual(status["websites_found"], 3)
        self.assertEqual(status["mode"], "ai_bulk")
        self.assertEqual(status["company_id"], "acme-id-1")
        self.assertIn("final_report.json", status["available_files"])
        self.assertNotIn("report.json", status["available_files"])

        runs = ai_mode_service.list_ai_mode_runs()
        self.assertIn(run_id, [r.get("run_id") for r in runs])

        path = ai_mode_service.get_ai_mode_result_path(run_id, "found.csv")
        self.assertEqual(path, run_dir / "found.csv")
        with self.assertRaises(ValueError):
            ai_mode_service.get_ai_mode_result_path(run_id, "report.json")

    def test_scrape_failure_yields_error_rows_and_partial_status(self):
        def boom(self, query, extra_params=None):
            raise RuntimeError("scrape.do down")

        with mock.patch.object(FakeScrapeDoClient, "search_google_ai_mode", boom):
            info = self._run("ai_deep")
        run_id = info["run_id"]
        status = ai_mode_service.get_ai_mode_status(run_id)
        self.assertEqual(status["status"], "completed_with_errors")
        self.assertEqual(status["failed_request_count"], 2)
        run_dir = self.results_root / "acme-corp" / run_id
        with (run_dir / "notFound.csv").open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 6)
        self.assertIn("scrape.do error", rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
