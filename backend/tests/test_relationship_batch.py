import unittest
from unittest.mock import patch

from app.services.serpwow import engine
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
                  "confidence_score": 90, "reason": "r", "extra_flags": []}
        status = engine._apply_batch_parsed_to_row(
            row, parsed, {"promptTokenCount": 10, "candidatesTokenCount": 5}, "m")
        self.assertEqual(status, "completed")
        self.assertEqual(row["result"]["official_website"], "https://modal.com/")
        ctx = row["result"]["context"]
        self.assertEqual(ctx["relationship"]["status"], "confirmed")
        self.assertEqual(ctx["gemini_batch_ai"]["raw"], parsed)
        self.assertIsNone(row["error"])

    def test_not_confirmed_fails_row_with_relationship_error(self):
        row = _rel_row()
        parsed = {"relationship_status": "not_confirmed",
                  "relationship_summary": "No relation found.",
                  "official_website": "https://modal.com/",
                  "confidence_score": 30, "reason": "r", "extra_flags": []}
        status = engine._apply_batch_parsed_to_row(row, parsed, {}, "m")
        self.assertEqual(status, "failed")
        self.assertIsNone(row["result"]["official_website"])
        self.assertEqual(row["error"], REL_ERROR_NOT_CONFIRMED)
        flags = row["result"]["context"]["relationship"]["flags"]
        self.assertTrue(any(f["flag"] == "url_found_no_relationship" for f in flags))

    def test_x_domain_pick_is_rejected(self):
        row = _rel_row(candidates=("https://www.eastlinkcap.com/team",))
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://www.eastlinkcap.com/team",
                  "relationship_summary": "s", "confidence_score": 80,
                  "reason": "r", "extra_flags": []}
        status = engine._apply_batch_parsed_to_row(row, parsed, {}, "m")
        self.assertEqual(status, "failed")
        self.assertIsNone(row["result"]["official_website"])


if __name__ == "__main__":
    unittest.main()
