# backend/tests/test_engine_smoke.py
"""Offline end-to-end smoke test for the unified two-mode AI engine.

Drives the broker engine in-process (tests/ai_mode_drive.py emulates publish ->
consume -> finish, no RabbitMQ) with the module seams (ScrapeDoClient +
make_llm_client) mocked so no network access is needed, and proves mode-specific
batching, the on-disk layout, and the unified output schema.
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
from app.services.ai_mode import worker as ai_worker
from app.services.ai_mode.models import TokenUsage
from tests.ai_mode_drive import drive_run

CSV_SIX = "company_name,country\n" + "".join(f"Company {i},Japan\n" for i in range(1, 7))

FAKE_ENV = {
    "SCRAPEDO_TOKEN": "fake-token",
    "GEMINI_API_KEY": "fake-key",
    "LLM_BATCH": "",
    "AI_MODE_STATUS_FLUSH_SEC": "0",
    # Keep the smoke test offline: unset so the S3 mirror no-ops (a populated
    # .env would otherwise attempt a live S3 upload).
    "S3_BUCKET": "",
    # Unset so the Slack notifier no-ops (a populated .env would otherwise POST
    # to a live webhook). Notify tests assert the calls.
    "SLACK_WEBHOOK_URL": "",
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
        for env in ("AI_BULK_BATCH_SIZE", "AI_DEEP_BATCH_SIZE"):
            os.environ.pop(env, None)
        ai_worker._reset_for_tests()
        self.results_root = results_root

    def tearDown(self):
        ai_worker._reset_for_tests()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _run(self, mode_key):
        info = ai_mode_service.prepare_ai_mode_run(
            CSV_SIX.encode("utf-8"), "input.csv",
            mode_key=mode_key, company_name="Acme Corp", company_id="acme-id-1",
        )
        drive_run(info["run_id"])
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

    def test_outputs_carry_every_input_column_end_to_end(self):
        """The wiring the writer's own tests can't see: run_ai_mode_finish must hand
        StreamingRunReport the resolved company column, or the whole passthrough silently
        reverts to the fixed seven. The blank-name row is here on purpose — it is dropped
        by parse_entities_csv, so a cursor that doesn't drop it too shifts every row
        after it onto the wrong company."""
        csv_text = ("firm_id,company_name,country,notes\n"
                    "f1,Company 1,Japan,alpha\n"
                    "f2,Company 2,Japan,beta\n"
                    ",,Japan,DROPPED — no company name\n"
                    "f3,Company 3,Japan,gamma\n")
        info = ai_mode_service.prepare_ai_mode_run(
            csv_text.encode("utf-8"), "input.csv",
            mode_key="ai_bulk", company_name="Acme Corp", company_id="acme-id-1")
        drive_run(info["run_id"])
        run_dir = self.results_root / "acme-corp" / info["run_id"]

        with (run_dir / "found.csv").open(newline="", encoding="utf-8-sig") as fh:
            found = list(csv.DictReader(fh))
        with (run_dir / "notFound.csv").open(newline="", encoding="utf-8-sig") as fh:
            not_found = list(csv.DictReader(fh))
        self.assertEqual(list(found[0]),
                         ["firm_id", "company_name", "country", "notes",
                          "website_url", "confidence", "flags", "attempt_log"])
        self.assertEqual(list(not_found[0]), list(found[0]) + ["error"])
        # Odd snos are found, even are not — so sno 1 (Company 1) and sno 3 (Company 3,
        # the row AFTER the dropped one) land in found.csv with their own cells.
        self.assertEqual([(r["firm_id"], r["company_name"], r["notes"]) for r in found],
                         [("f1", "Company 1", "alpha"), ("f3", "Company 3", "gamma")])
        self.assertEqual([(r["firm_id"], r["notes"]) for r in not_found],
                         [("f2", "beta")])

    def test_outputs_layout_and_schema(self):
        info = self._run("ai_bulk")
        run_id = info["run_id"]
        run_dir = self.results_root / "acme-corp" / run_id
        self.assertTrue(run_dir.is_dir())

        for name in ("input.csv", "found.csv", "notFound.csv", "final_report.json",
                     "run.log", "status.json"):
            self.assertTrue((run_dir / name).exists(), name)
        self.assertTrue((run_dir / "raw_responses" / "request_000001.json").exists())
        self.assertFalse((run_dir / "report.json").exists())
        self.assertFalse((run_dir / "ai_mode_debug.log").exists())

        with (run_dir / "found.csv").open(newline="", encoding="utf-8-sig") as fh:
            found_rows = list(csv.DictReader(fh))
        with (run_dir / "notFound.csv").open(newline="", encoding="utf-8-sig") as fh:
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
        self.assertEqual(report["requests"][0]["raw_json_file"], "raw_responses/request_000001.json")
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

    def test_notify_run_complete_fires_on_success(self):
        with mock.patch("app.core.notify.notify_run_complete") as done, \
                mock.patch("app.core.notify.notify_run_failed") as failed:
            self._run("ai_bulk")
        failed.assert_not_called()
        done.assert_called_once()
        kw = done.call_args.kwargs
        self.assertEqual(kw["status"], "completed")
        self.assertEqual(kw["company"], "Acme Corp")
        self.assertEqual(kw["found"], 3)
        self.assertEqual(kw["not_found"], 3)
        self.assertEqual(kw["total_rows"], 6)

    def test_notify_run_failed_fires_on_crash(self):
        # Force run_ai_mode_finish's failure path: assemble (the Phase-3
        # streaming report constructor) raises.
        with mock.patch.object(ai_mode_service, "StreamingRunReport",
                               side_effect=RuntimeError("disk full")), \
                mock.patch("app.core.notify.notify_run_complete") as done, \
                mock.patch("app.core.notify.notify_run_failed") as failed:
            info = self._run("ai_bulk")
        done.assert_not_called()
        failed.assert_called_once()
        self.assertEqual(
            ai_mode_service.get_ai_mode_status(info["run_id"])["status"], "failed")

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
        with (run_dir / "notFound.csv").open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 6)
        self.assertIn("scrape.do error", rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
