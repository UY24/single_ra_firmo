import csv
import json
import tempfile
import unittest
from pathlib import Path

from app.services.serpwow import reporting


def _state():
    return {
        "upload_id": "up1", "company_name": "Acme Motors", "pipeline": "gsearch",
        "status": "completed_with_errors", "processing_seconds_total": 3.5,
        "rows": [
            {"row_index": 0, "company_name": "Acme Motors", "country": "us", "status": "completed",
             "error": None, "result": {
                 "official_website": "https://acme-motors.com", "gemini_cost_usd": 0.0001,
                 "context": {
                     "cost_breakdown": {"serpwow_request_count": 4},
                     "formatted_results": [
                         {"phase": "phase1_exact_hook", "query": "q1", "success": True,
                          "error": None, "search_url": "http://s/1"}],
                     "final_url_selection_ai": {"used": True, "usage": {
                         "promptTokenCount": 100, "candidatesTokenCount": 20}, "raw": {
                         "official_website": "https://acme-motors.com", "confidence_score": 88,
                         "confidence": "high", "reason": "match", "alternatives": ["https://x.com"]}},
                 }}},
            {"row_index": 1, "company_name": "NoWeb Co", "country": "us", "status": "failed",
             "error": "not found", "result": {
                 "official_website": None, "gemini_cost_usd": 0.0,
                 "context": {"cost_breakdown": {"serpwow_request_count": 6},
                             "formatted_results": [],
                             "final_url_selection_ai": {"used": True, "usage": {},
                                 "raw": {"official_website": None, "confidence_score": 0,
                                         "confidence": "low", "reason": "none"}}}}},
        ],
    }


class TestGsearchReporting(unittest.TestCase):
    def test_entity_results_and_summary(self):
        state = _state()
        results = reporting.state_to_entity_results(state)
        self.assertEqual(results[0].website_url, "https://acme-motors.com")
        self.assertEqual(results[0].confidence, 88)
        self.assertIsNone(results[1].website_url)
        summary = reporting.build_summary(state, results)
        self.assertEqual(summary["websites_found"], 1)
        self.assertEqual(summary["websites_not_found"], 1)
        self.assertEqual(summary["cost"]["serpwow_searches"], 10)
        self.assertEqual(summary["token_usage"]["prompt_tokens"], 100)

    def test_website_from_validated_result_and_mismatch_flag(self):
        state = {
            "upload_id": "u", "company_name": "C", "pipeline": "gsearch",
            "status": "completed_with_errors",
            "rows": [
                # rejected pick: result.official_website None, raw still holds a URL -> NOT found
                {"row_index": 0, "company_name": "A", "country": "us", "status": "failed",
                 "error": "x", "result": {"official_website": None, "context": {
                     "cost_breakdown": {},
                     "gemini_batch_ai": {"raw": {"official_website": "https://rejected.com",
                                                 "confidence_score": 50}}}}},
                # kept but domain-mismatch flagged: result.official_website set -> found + flag
                {"row_index": 1, "company_name": "B", "country": "us", "status": "completed",
                 "error": None, "result": {"official_website": "https://brand.com", "context": {
                     "cost_breakdown": {},
                     "gemini_batch_ai": {"raw": {"official_website": "https://brand.com",
                                                 "confidence_score": 70,
                                                 "domain_name_mismatch": True}}}}},
            ],
        }
        results = reporting.state_to_entity_results(state)
        self.assertIsNone(results[0].website_url)          # rejected -> not found
        self.assertEqual(results[1].website_url, "https://brand.com")
        self.assertTrue(any(f.flag == "domain_name_mismatch" for f in results[1].flags))
        summary = reporting.build_summary(state, results)
        self.assertEqual(summary["websites_found"], 1)
        self.assertEqual(summary["websites_not_found"], 1)

    def test_summary_model_and_batch_mode(self):
        # per-row mode: model comes from final_url_selection_ai, no gemini_batch block
        state = _state()
        state["rows"][0]["result"]["context"]["final_url_selection_ai"]["model"] = "gemini-2.5-flash-lite"
        summary = reporting.build_summary(state, reporting.state_to_entity_results(state))
        self.assertEqual(summary["model"], "gemini-2.5-flash-lite")
        self.assertFalse(summary["is_batch"])
        # a model ran -> confidence_mode is "llm"
        self.assertEqual(summary["confidence_mode"], "llm")
        # batch mode: presence of the gemini_batch block flips is_batch
        state["gemini_batch"] = {"status": "succeeded"}
        summary = reporting.build_summary(state, reporting.state_to_entity_results(state))
        self.assertTrue(summary["is_batch"])

    def test_summary_confidence_mode_heuristic(self):
        # no LLM model surfaced (e.g. gmaps heuristic) -> confidence_mode is "heuristic"
        state = _state()
        summary = reporting.build_summary(state, reporting.state_to_entity_results(state))
        self.assertIsNone(summary["model"])
        self.assertEqual(summary["confidence_mode"], "heuristic")

    def test_write_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            paths = reporting.write_outputs(Path(d), _state())
            self.assertTrue(paths["found.csv"].exists())
            self.assertTrue(paths["notFound.csv"].exists())
            self.assertTrue(paths["report.json"].exists())
            self.assertTrue(paths["run.log"].exists())
            with paths["found.csv"].open() as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(rows[0]["website_url"], "https://acme-motors.com")
            self.assertEqual(rows[0]["confidence"], "88")
            report = json.loads(paths["report.json"].read_text())
            self.assertEqual(report["summary"]["websites_found"], 1)
            self.assertEqual(len(report["rows"]), 2)
            self.assertIn("acme-motors.com", paths["run.log"].read_text())

    def test_run_log_header(self):
        """Assert run.log starts with a 4-line header (status, counts, cost, blank line)."""
        with tempfile.TemporaryDirectory() as d:
            state = _state()
            paths = reporting.write_outputs(Path(d), state)
            log_text = paths["run.log"].read_text()
            lines = log_text.split("\n")
            # Header should be: status line, counts line, cost line, blank line
            self.assertIn("# gsearch run up1 — status=completed_with_errors", lines[0])
            self.assertIn("# rows=2 found=1 not_found=1 batch=False model=None", lines[1])
            self.assertIn("# cost: llm_usd=", lines[2])
            self.assertIn("serpwow_usd=", lines[2])
            self.assertIn("total_usd=", lines[2])
            self.assertIn("serpwow_searches=10", lines[2])
            self.assertEqual(lines[3], "")  # blank line
            # per-row lines follow
            self.assertIn("[1]", lines[4])
            self.assertIn("Acme Motors", lines[4])
