import unittest
from unittest.mock import patch

from app.services.serpwow import engine
from app.services.serpwow import outcomes as o
from app.services.serpwow.constants import REL_ERROR_NOT_CONFIRMED


def _rel_row(row_index=1, status="completed", skip_llm=False,
             candidates=("https://modal.com/",), x_name="eastlinkcap"):
    return {
        "row_index": row_index, "company_name": "Modal", "country": "",
        "x_name": x_name, "input_url": "https://www.eastlinkcap.com/p", "city": "",
        "status": status, "error": "Pending Gemini batch post-processing decision.",
        "result": {
            "official_website": None, "gemini_cost_usd": 0.0, "total_cost_usd": 0.02,
            "context": {
                "pipeline": "relationship", "skip_llm": skip_llm,
                "x_domain": "eastlinkcap.com",
                "candidates": list(candidates),
                "candidate_evidence": [{
                    "evidence_id": "candidate-1", "url": candidates[0],
                    "phase": "phase1", "source_field": "knowledge_graph.website",
                }] if candidates else [],
                "evidence": [{
                    "evidence_id": "relationship-1",
                    "text": "Eastlink invests in Modal.",
                    "phase": "phase1", "source_field": "ai_overview",
                }],
                "ai_overview_texts": ["Eastlink invests in Modal."],
                "search_attempts": [{"attempt": "phase1_relationship", "query": "q"}],
                "phase4_hit": True,
                "relationship": {"status": "pending", "summary": "",
                                 "verified_pair": "eastlinkcap ↔ Modal", "flags": []},
                "cost_breakdown": {"serpwow_request_count": 4},
            },
        },
    }


class TestRelationshipBatchPrompt(unittest.TestCase):
    def test_prompt_dispatches_to_relationship_builder(self):
        prompt = engine._build_batch_prompt_for_row(_rel_row())
        self.assertIn("relationship_status", prompt)
        self.assertIn("eastlinkcap", prompt)
        self.assertIn("Modal", prompt)
        self.assertIn("modal.com", prompt)
        # Batch parity: the x_domain from context is threaded into the prompt too.
        self.assertIn("eastlinkcap.com", prompt)
        self.assertIn("company_x_domain", prompt)
        self.assertIn("Candidate Evidence", prompt)
        self.assertIn("candidate-1", prompt)
        self.assertIn("Supplied Relationship Evidence", prompt)
        self.assertIn("relationship-1", prompt)

    def test_gsearch_prompt_unchanged(self):
        row = {"row_index": 1, "company_name": "Acme", "country": "US",
               "status": "completed",
               "result": {"official_website": None,
                          "context": {"pipeline": "gsearch",
                                      "candidates": ["https://acme.com/"]}}}
        prompt = engine._build_batch_prompt_for_row(row)
        self.assertIn("company website resolver", prompt)


class TestRelationshipBatchItems(unittest.TestCase):
    def test_skip_llm_rows_are_not_seeded(self):
        state = {"rows": [_rel_row(1), _rel_row(2, status="failed", skip_llm=True)]}
        items, by_key = engine._build_batch_items_for_state(state)
        self.assertEqual([k for k, _ in items], ["row-1"])
        self.assertEqual(by_key, {"row-1": 1})


class TestRelationshipBatchApply(unittest.TestCase):
    def test_confirmed_result_completes_row_with_url(self):
        row = _rel_row()
        parsed = {"relationship_status": "confirmed",
                  "relationship_summary": "Eastlink invested in Modal.",
                  "official_website": "https://modal.com/",
                  "confidence_score": 90, "reason": "r",
                  "supporting_evidence_ids": ["relationship-1"],
                  "extra_flags": []}
        status = engine._apply_batch_parsed_to_row(
            row, parsed, {"promptTokenCount": 10, "candidatesTokenCount": 5}, "m")
        self.assertEqual(status, "completed")
        self.assertEqual(row["result"]["official_website"], "https://modal.com")
        ctx = row["result"]["context"]
        self.assertEqual(ctx["relationship"]["status"], "confirmed")
        self.assertEqual(ctx["relationship"]["evidence"], [ctx["evidence"][0]])
        self.assertEqual(ctx["gemini_batch_ai"]["raw"], parsed)
        self.assertIsNone(row["error"])

    def test_not_confirmed_completes_row_as_not_found_with_relationship_error(self):
        # "Not confirmed" is a valid business outcome (no relationship evidence),
        # not a technical error -> status="completed", outcome="not_found" (Task 4).
        row = _rel_row()
        parsed = {"relationship_status": "not_confirmed",
                  "relationship_summary": "No relation found.",
                  "official_website": "https://modal.com/",
                  "confidence_score": 30, "reason": "r", "extra_flags": []}
        status = engine._apply_batch_parsed_to_row(row, parsed, {}, "m")
        self.assertEqual(status, "completed")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["outcome"], o.OUTCOME_NOT_FOUND)
        self.assertIsNone(row["error_source"])
        self.assertIsNone(row["error_category"])
        self.assertIsNone(row["result"]["official_website"])
        self.assertEqual(row["error"], REL_ERROR_NOT_CONFIRMED)
        flags = row["result"]["context"]["relationship"]["flags"]
        self.assertTrue(any(f["flag"] == "url_found_no_relationship" for f in flags))

    def test_x_domain_pick_is_rejected(self):
        # Confirmed-but-invalid (X's own domain) is also a valid not_found, not an error.
        row = _rel_row(candidates=("https://www.eastlinkcap.com/team",))
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://www.eastlinkcap.com/team",
                  "relationship_summary": "s", "confidence_score": 80,
                  "supporting_evidence_ids": ["relationship-1"],
                  "reason": "r", "extra_flags": []}
        status = engine._apply_batch_parsed_to_row(row, parsed, {}, "m")
        self.assertEqual(status, "completed")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["outcome"], o.OUTCOME_NOT_FOUND)
        self.assertIsNone(row["result"]["official_website"])

    def test_candidate_only_citation_cannot_confirm_and_stores_accepted_record(self):
        row = _rel_row()
        parsed = {"relationship_status": "confirmed",
                  "relationship_summary": "Unsupported conclusion.",
                  "official_website": "https://modal.com/",
                  "confidence_score": 90,
                  "supporting_evidence_ids": ["candidate-1"],
                  "reason": "r", "extra_flags": ["model-note"]}
        engine._apply_batch_parsed_to_row(row, parsed, {}, "m")
        ctx = row["result"]["context"]
        self.assertEqual(ctx["relationship"]["status"], "unclear")
        self.assertIsNone(row["result"]["official_website"])
        self.assertEqual(parsed["confidence_score"], 0)
        self.assertEqual(ctx["relationship"]["evidence"], [ctx["candidate_evidence"][0]])
        self.assertEqual(
            [flag["flag"] for flag in ctx["relationship"]["flags"]],
            ["confirmed_without_evidence_id", "relationship_unclear", "model-note"],
        )

    def test_batch_stores_accepted_records_in_supplied_order(self):
        row = _rel_row()
        parsed = {"relationship_status": "confirmed",
                  "relationship_summary": "Grounded.",
                  "official_website": "https://modal.com/",
                  "confidence_score": 90,
                  "supporting_evidence_ids": [
                      "relationship-1", "missing", "candidate-1"],
                  "reason": "r", "extra_flags": []}
        engine._apply_batch_parsed_to_row(row, parsed, {}, "m")
        ctx = row["result"]["context"]
        self.assertEqual(ctx["relationship"]["evidence"], [
            ctx["candidate_evidence"][0], ctx["evidence"][0],
        ])
        self.assertEqual(parsed["supporting_evidence_ids"],
                         ["relationship-1", "candidate-1"])
        self.assertTrue(any(
            flag["flag"] == "unknown_evidence_id"
            for flag in ctx["relationship"]["flags"]
        ))


if __name__ == "__main__":
    unittest.main()
