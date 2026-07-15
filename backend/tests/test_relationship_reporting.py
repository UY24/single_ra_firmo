# backend/tests/test_relationship_reporting.py
import csv
import json
import tempfile
import unittest
from pathlib import Path

from app.services.serpwow import serpwow_reporting
from app.services.serpwow.constants import (
    REL_ERROR_CONFIRMED_URL_INVALID,
    REL_REASON_CONFIRMED_URL_NOT_VALIDATED,
)


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
                                 "reason_code": "" if official else "relationship_not_confirmed",
                                 "evidence": [{
                                     "evidence_id": f"relationship-{row_index}",
                                     "text": f"{x} backed {y}, in Montréal.",
                                 }],
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
            with open(paths["found.csv"], encoding="utf-8") as csv_file:
                found = list(csv.DictReader(csv_file))
            with open(paths["notFound.csv"], encoding="utf-8") as csv_file:
                not_found = list(csv.DictReader(csv_file))
            with open(paths["skipped.csv"], encoding="utf-8") as csv_file:
                skipped = list(csv.DictReader(csv_file))
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
            self.assertEqual(row["reason_code"], "")
            self.assertEqual(json.loads(row["supporting_evidence"])[0]["evidence_id"],
                             "relationship-1")
            self.assertIn("Montréal", row["supporting_evidence"])
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
        self.assertEqual(summary["searchable_rows"], 5)
        self.assertEqual(sum(summary["outcome_breakdown"].values()), summary["searchable_rows"])

    def test_report_json_rows_include_relationship(self):
        with tempfile.TemporaryDirectory() as td:
            paths = serpwow_reporting.write_outputs(Path(td), _state())
            report = json.loads(Path(paths["report.json"]).read_text())
            self.assertEqual(report["summary"]["unique_pairs"], 2)
            self.assertEqual(report["rows"][0]["relationship_status"], "confirmed")
            self.assertEqual(report["rows"][0]["reason_code"], "")
            self.assertIsInstance(report["rows"][0]["supporting_evidence"], list)
            self.assertEqual(
                report["rows"][0]["supporting_evidence"][0]["evidence_id"],
                "relationship-1",
            )

    def test_confirmed_without_url_is_fully_preserved_in_not_found_artifacts(self):
        state = _state()
        pair = state["rows"][1]
        pair["status"] = "completed"
        pair["error"] = REL_ERROR_CONFIRMED_URL_INVALID
        relationship = pair["result"]["context"]["relationship"]
        relationship.update({
            "status": "confirmed",
            "summary": "Dobbs invested in GR TRDCX.",
            "reason_code": REL_REASON_CONFIRMED_URL_NOT_VALIDATED,
            "evidence": [{
                "evidence_id": "relationship-confirmed-1",
                "text": "Dobbs invested in GR TRDCX, Montréal division.",
            }],
            "flags": [{
                "flag": REL_REASON_CONFIRMED_URL_NOT_VALIDATED,
                "why": "relationship confirmed but no supplied candidate URL passed validation",
            }],
        })
        pair["result"]["context"]["final_url_selection_ai"]["raw"]["confidence_score"] = 83

        with tempfile.TemporaryDirectory() as td:
            paths = serpwow_reporting.write_outputs(Path(td), state)
            with open(paths["notFound.csv"], encoding="utf-8") as csv_file:
                not_found = list(csv.DictReader(csv_file))
            report = json.loads(Path(paths["report.json"]).read_text(encoding="utf-8"))
            run_log = Path(paths["run.log"]).read_text(encoding="utf-8")

        self.assertEqual(len(not_found), 1)
        row = not_found[0]
        self.assertEqual(row["website_url"], "")
        self.assertEqual(row["relationship_status"], "confirmed")
        self.assertEqual(row["relationship_summary"], "Dobbs invested in GR TRDCX.")
        self.assertEqual(row["confidence"], "83")
        self.assertEqual(row["reason_code"], REL_REASON_CONFIRMED_URL_NOT_VALIDATED)
        evidence = json.loads(row["supporting_evidence"])
        self.assertEqual(evidence[0]["evidence_id"], "relationship-confirmed-1")
        self.assertIn("Montréal division", evidence[0]["text"])
        self.assertIn(REL_REASON_CONFIRMED_URL_NOT_VALIDATED, row["flags"])
        self.assertIn("phase1_relationship", row["attempt_log"])
        self.assertEqual(row["verified_pair"], "dobbs ↔ GR TRDCX")
        self.assertEqual(row["error"], REL_ERROR_CONFIRMED_URL_INVALID)

        report_row = report["rows"][-1]
        self.assertEqual(report_row["reason_code"], REL_REASON_CONFIRMED_URL_NOT_VALIDATED)
        self.assertEqual(report_row["supporting_evidence"], evidence)
        self.assertIn(REL_REASON_CONFIRMED_URL_NOT_VALIDATED, run_log)

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
