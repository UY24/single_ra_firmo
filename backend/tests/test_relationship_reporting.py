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
                                 "verified_pair": f"{x} ↔ {y}",
                                 "flags": list(flags)},
                "final_url_selection_ai": {
                    "model": "gemini-2.5-flash-lite",
                    "usage": {"promptTokenCount": 10, "candidatesTokenCount": 5},
                    "raw": {"confidence_score": 88, "confidence": "high",
                            "reason": "why"},
                },
                "formatted_results": [
                    {"phase": "phase1_relationship", "query": "q1", "success": True,
                     "error": None, "search_url": "https://g/1"}],
                "cost_breakdown": {"serpwow_request_count": 4},
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
            self.assertIn("skipped.csv", paths)
            found = list(csv.DictReader(open(paths["found.csv"])))
            not_found = list(csv.DictReader(open(paths["notFound.csv"])))
            skipped = list(csv.DictReader(open(paths["skipped.csv"])))
            self.assertEqual(len(found), 2)       # both Modal duplicate rows
            self.assertEqual(len(not_found), 1)
            self.assertEqual(len(skipped), 1)
            row = found[0]
            # passthrough columns preserved
            self.assertEqual(row["Box_No"], "2")
            self.assertEqual(row["OCR_Status"], "SUCCESS")
            # output columns
            self.assertEqual(row["website_url"], "https://modal.com/")
            self.assertEqual(row["relationship_status"], "confirmed")
            self.assertEqual(row["relationship_summary"], "summary Modal")
            self.assertEqual(row["confidence"], "88")
            self.assertEqual(row["verified_pair"], "eastlinkcap ↔ Modal")
            self.assertIn("attempt_log", row)
            nf = not_found[0]
            self.assertIn("url_found_no_relationship", nf["flags"])
            self.assertIn("error", nf)
            self.assertEqual(skipped[0]["skip_reason"], "blank_company_name_y")

    def test_summary_gains_relationship_fields(self):
        state = _state()
        results = serpwow_reporting.state_to_entity_results(state)
        summary = serpwow_reporting.build_summary(state, results)
        self.assertEqual(summary["total_rows"], 4)          # original CSV rows
        self.assertEqual(summary["blank_rows"], 1)
        self.assertEqual(summary["searchable_rows"], 3)
        self.assertEqual(summary["unique_pairs"], 2)
        self.assertEqual(summary["websites_found"], 2)
        self.assertEqual(summary["websites_not_found"], 1)
        self.assertEqual(summary["relationship_breakdown"],
                         {"confirmed": 2, "not_confirmed": 1, "unclear": 0})
        self.assertEqual(summary["confidence_mode"], "llm")
        self.assertEqual(summary["cost"]["serpwow_searches"], 8)

    def test_report_json_rows_include_relationship(self):
        with tempfile.TemporaryDirectory() as td:
            paths = serpwow_reporting.write_outputs(Path(td), _state())
            report = json.loads(Path(paths["report.json"]).read_text())
            self.assertEqual(report["summary"]["unique_pairs"], 2)
            self.assertEqual(report["rows"][0]["relationship_status"], "confirmed")

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
