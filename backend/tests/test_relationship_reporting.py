# backend/tests/test_relationship_reporting.py
import csv
import json
import tempfile
import unittest
from pathlib import Path

from app.services.serpwow import serpwow_reporting


def _pair_row(row_index, y, x, source_rows, official, rel_status, error=None,
              flags=(), skip_llm=False):
    return {
        "row_index": row_index, "company_name": y, "country": "",
        "x_name": x, "input_url": "", "city": "",
        "source_row_indices": list(source_rows),
        "status": "completed" if official else "failed", "error": error,
        "result": {
            "official_website": official,
            "gemini_cost_usd": 0.001, "total_cost_usd": 0.021,
            "context": {
                "pipeline": "relationship", "skip_llm": skip_llm,
                "candidates": [official] if official else [],
                "relationship": {"status": rel_status, "summary": f"summary {y}",
                                 "resolved_company_y_name": f"Resolved {y}",
                                 "evidence": [f"evidence {y} one", f"evidence {y} two"],
                                 "relationship_confidence_score": 96,
                                 "website_confidence_score": 88 if official else 0,
                                 "verified_pair": f"{x} ↔ {y}",
                                 "flags": list(flags)},
                "final_url_selection_ai": {
                    "model": "gemini-2.5-flash-lite",
                    "usage": {"promptTokenCount": 10, "candidatesTokenCount": 5},
                    "raw": {"confidence_score": 88, "confidence": "high",
                            "relationship_confidence_score": 96,
                            "website_confidence_score": 88 if official else 0,
                            "reason": "why"},
                },
                "formatted_results": [
                    {"phase": "phase1_relationship", "query": "q1", "success": True,
                     "error": None, "search_url": "https://g/1",
                     "result": "AI overview returned; 1 candidate(s)"},
                    {"phase": "phase2_financial_event_evidence", "query": "q2",
                     "success": True, "error": None, "search_url": "https://g/2"},
                    {"phase": "phase3_portfolio_identity", "query": "q3",
                     "success": True, "error": None, "search_url": "https://g/3"}],
                "cost_breakdown": {"serpwow_request_count": 3},
            },
        },
    }


def _state():
    header = ["Input_URL", "Company_Name_X", "Box_No", "Company_Name_Y", "OCR_Status"]
    original_rows = [
        {"Input_URL": "https://m25vc.com/p", "Company_Name_X": "m25vc",
         "Box_No": "1", "Company_Name_Y": "", "OCR_Status": "NO_TEXT"},          # 0 blank
        {"Input_URL": "https://eastlinkcap.com/p", "Company_Name_X": "eastlinkcap",
         "Box_No": "2", "Company_Name_Y": "Modal", "OCR_Status": "SUCCESS"},     # 1
        {"Input_URL": "https://eastlinkcap.com/p", "Company_Name_X": "eastlinkcap",
         "Box_No": "3", "Company_Name_Y": "Modal", "OCR_Status": "SUCCESS"},     # 2 dup
        {"Input_URL": "https://dobbs.com/p", "Company_Name_X": "dobbs",
         "Box_No": "4", "Company_Name_Y": "GR TRDCX", "OCR_Status": "SUCCESS"},  # 3
    ]
    return {
        "upload_id": "u1", "company_name": "Acme", "pipeline": "relationship",
        "status": "completed_with_errors", "processing_seconds_total": 12.5,
        "relationship": {"header": header, "original_rows": original_rows,
                         "blank_row_indices": [0], "blank_rows": 1,
                         "row_count_original": 4},
        "rows": [
            _pair_row(1, "Modal", "eastlinkcap", [1, 2],
                      "https://modal.com/", "confirmed"),
            _pair_row(2, "GR TRDCX", "dobbs", [3], None, "not_confirmed",
                      error="No financial relationship confirmed between Company X and Company Y.",
                      flags=[{"flag": "url_found_no_relationship",
                              "why": "candidate URL https://x.example found but relationship is not_confirmed"}]),
        ],
    }


class TestRelationshipReporting(unittest.TestCase):
    def test_entity_results_fan_out_to_original_rows(self):
        results = serpwow_reporting.state_to_entity_results(_state())
        # 3 searchable original rows (blank row is NOT in found/notFound)
        self.assertEqual(len(results), 3)
        self.assertEqual([r.website_url for r in results],
                         ["https://modal.com/", "https://modal.com/", None])

    def test_write_outputs_files_and_columns(self):
        with tempfile.TemporaryDirectory() as td:
            paths = serpwow_reporting.write_outputs(Path(td), _state())
            # Files split by RELATIONSHIP STATUS, not URL presence. No skipped.csv.
            self.assertNotIn("skipped.csv", paths)
            with Path(paths["confirmed_relation.csv"]).open() as fh:
                confirmed = list(csv.DictReader(fh))
            with Path(paths["notconfirmed_relation.csv"]).open() as fh:
                notconfirmed = list(csv.DictReader(fh))
            self.assertEqual(len(confirmed), 2)       # both Modal duplicate rows (confirmed)
            self.assertEqual(len(notconfirmed), 1)    # GR TRDCX (not_confirmed)
            row = confirmed[0]
            # passthrough columns preserved
            self.assertEqual(row["Box_No"], "2")
            self.assertEqual(row["OCR_Status"], "SUCCESS")
            # output columns
            self.assertEqual(row["website_url"], "https://modal.com/")
            self.assertEqual(row["relationship_status"], "confirmed")
            self.assertEqual(row["relationship_summary"], "summary Modal")
            self.assertEqual(row["resolved_company_y_name"], "Resolved Modal")
            self.assertEqual(row["relationship_evidence"],
                             "evidence Modal one\nevidence Modal two")
            self.assertEqual(row["relationship_confidence"], "96")
            self.assertEqual(row["website_confidence"], "88")
            self.assertEqual(row["phases_used"], "3")
            self.assertEqual(row["confidence"], "88")
            self.assertEqual(row["verified_pair"], "eastlinkcap ↔ Modal")
            self.assertIn("attempt_log", row)
            self.assertIn("AI overview returned; 1 candidate(s)", row["attempt_log"])
            nc = notconfirmed[0]
            self.assertEqual(nc["relationship_status"], "not_confirmed")
            self.assertIn("url_found_no_relationship", nc["flags"])
            self.assertIn("error_reason", nc)

    def test_confirmed_relationship_without_url_stays_in_confirmed_file(self):
        # Split is by relationship status: a confirmed row with no URL still lands in
        # confirmed_relation.csv (with website_url empty) — the missing URL shows up in
        # the report's websites_not_found count, not by moving the row.
        state = _state()
        state["relationship"]["original_rows"] = [
            {"Input_URL": "https://eastlinkcap.com/p", "Company_Name_X": "eastlinkcap",
             "Box_No": "2", "Company_Name_Y": "Modal", "OCR_Status": "SUCCESS"}]
        state["relationship"]["row_count_original"] = 1
        state["relationship"]["blank_row_indices"] = []
        state["relationship"]["blank_rows"] = 0
        state["rows"] = [_pair_row(
            1, "Modal", "eastlinkcap", [0], None, "confirmed",
            error="Financial relationship confirmed, but no valid Company Y website URL was found.")]

        with tempfile.TemporaryDirectory() as td:
            paths = serpwow_reporting.write_outputs(Path(td), state)
            with Path(paths["confirmed_relation.csv"]).open() as fh:
                rows = list(csv.DictReader(fh))
            with Path(paths["notconfirmed_relation.csv"]).open() as fh:
                self.assertEqual(list(csv.DictReader(fh)), [])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["relationship_status"], "confirmed")
        self.assertEqual(rows[0]["website_url"], "")
        self.assertEqual(rows[0]["resolved_company_y_name"], "Resolved Modal")
        self.assertEqual(rows[0]["relationship_confidence"], "96")
        self.assertEqual(rows[0]["website_confidence"], "0")
        self.assertIn("evidence Modal one", rows[0]["relationship_evidence"])

    def test_summary_gains_relationship_fields(self):
        state = _state()
        results = serpwow_reporting.state_to_entity_results(state)
        summary = serpwow_reporting.build_summary(state, results)
        self.assertEqual(summary["total_rows"], 4)          # original CSV rows
        # dedup-era keys are gone (no unique_pairs / searchable_rows / blank_rows).
        self.assertNotIn("unique_pairs", summary)
        self.assertNotIn("searchable_rows", summary)
        self.assertEqual(summary["websites_found"], 2)
        self.assertEqual(summary["websites_not_found"], 1)
        self.assertEqual(summary["relationship_breakdown"],
                         {"confirmed": 2, "not_confirmed": 1, "unclear": 0})
        self.assertEqual(summary["confidence_mode"], "llm")
        self.assertEqual(summary["cost"]["serpwow_searches"], 6)

    def test_outcomes_fan_out_to_searchable_original_rows(self):
        state = _state()
        state["relationship"]["original_rows"].extend([{}, {}])
        state["relationship"]["row_count_original"] = 6
        found, not_found = state["rows"]
        found["source_row_indices"] = [1, 2]
        found["outcome"] = "found"
        not_found["source_row_indices"] = [3]
        not_found["status"] = "completed"
        not_found["outcome"] = "not_found"
        errored = _pair_row(3, "Broken", "x", [4, 5], None, "unclear", error="boom")
        errored.update({"outcome": "error", "error_source": "gemini", "error_category": "llm_error"})
        orphan = _pair_row(4, "Orphan", "x", [], None, "unclear", error="orphan")
        orphan["outcome"] = "error"
        state["rows"].extend([errored, orphan])

        results = serpwow_reporting.state_to_entity_results(state)
        summary = serpwow_reporting.build_summary(state, results)

        self.assertEqual(summary["outcome_breakdown"],
                         {"found": 2, "not_found": 1, "errored": 2})
        self.assertEqual(summary["error_breakdown"]["by_source"], {"gemini": 2})
        # Outcomes still fan out by source_row_indices (found 2 + not_found 1 + errored 2).
        self.assertEqual(sum(summary["outcome_breakdown"].values()), 5)

    def test_report_json_rows_include_relationship(self):
        with tempfile.TemporaryDirectory() as td:
            paths = serpwow_reporting.write_outputs(Path(td), _state())
            report = json.loads(Path(paths["report.json"]).read_text())
            self.assertEqual(report["summary"]["relationship_breakdown"],
                             {"confirmed": 2, "not_confirmed": 1, "unclear": 0})
            self.assertEqual(report["rows"][0]["relationship_status"], "confirmed")

    def test_provider_error_reason_is_clear_and_redacted_in_viewable_files(self):
        state = _state()
        state["relationship"]["original_rows"] = [
            {"Input_URL": "https://eastlinkcap.com/p",
             "Company_Name_X": "eastlinkcap", "Box_No": "2",
             "Company_Name_Y": "Modal", "OCR_Status": "SUCCESS"}]
        state["relationship"].update(
            row_count_original=1, blank_row_indices=[], blank_rows=0)
        unsafe = (
            "Server error '503 Service Unavailable' for url "
            "'https://api.serpwow.com/live/search?api_key=secret-key&q=x'")
        row = _pair_row(1, "Modal", "eastlinkcap", [0], None,
                        "not_confirmed", error=unsafe, skip_llm=True)
        row.update(outcome="error", error_source="serpwow",
                   error_category="http_5xx")
        row["result"]["context"]["formatted_results"] = [{
            "phase": "phase1_relationship_and_url", "query": "q1",
            "success": False, "status_code": 503, "error": unsafe,
            "result": unsafe, "search_url": None}]
        state["rows"] = [row]

        with tempfile.TemporaryDirectory() as td:
            paths = serpwow_reporting.write_outputs(Path(td), state)
            with Path(paths["notconfirmed_relation.csv"]).open() as fh:
                not_found = list(csv.DictReader(fh))[0]
            report = json.loads(Path(paths["report.json"]).read_text())
            run_log = Path(paths["run.log"]).read_text()

        expected = "SerpWow failed (HTTP 503): Service Unavailable."
        self.assertEqual(not_found["error_reason"], expected)
        self.assertEqual(report["rows"][0]["error_reason"], expected)
        self.assertIn(expected, run_log)
        self.assertNotIn("secret-key", json.dumps(report))
        self.assertNotIn("secret-key", not_found["attempt_log"])

    def test_gsearch_state_output_unchanged(self):
        gsearch_state = {
            "upload_id": "g1", "company_name": "Acme", "pipeline": "gsearch",
            "status": "completed",
            "rows": [{"row_index": 1, "company_name": "A", "country": "US",
                      "status": "completed", "error": None,
                      "result": {"official_website": "https://a.com/",
                                 "context": {"final_url_selection_ai": {
                                     "model": "m", "usage": {},
                                     "raw": {"confidence_score": 70}}}}}],
        }
        results = serpwow_reporting.state_to_entity_results(gsearch_state)
        self.assertEqual(len(results), 1)
        summary = serpwow_reporting.build_summary(gsearch_state, results)
        self.assertNotIn("unique_pairs", summary)


if __name__ == "__main__":
    unittest.main()
