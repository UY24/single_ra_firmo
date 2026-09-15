"""FIX 3 hardening test: gmaps reuses the SHARED gsearch batch machinery with no
new engine. Drives _build_batch_items_for_state / _apply_batch_parsed_to_row
directly (no network) against a gmaps row and asserts the result lands in
context["gemini_batch_ai"], which reporting reads ahead of
context["gmaps_confidence"] (see _confidence_raw's key order) -- locking in
that a gmaps batch row is reported exactly like a gsearch batch row.
"""
import unittest

from app.services.serpwow import engine as legacy_app
from app.services.serpwow import reporting


def _terminal_gmaps_state():
    """A terminal (status=completed) gmaps upload state: one row whose
    result.context has both "gmaps" (the Maps lookup) and "candidates" (the
    pre-deduped URL list _build_batch_prompt_for_row/gsearch's engine expects),
    plus a heuristic gmaps_confidence block already written (as gmaps always
    writes one before any LLM/batch step runs)."""
    return {
        "upload_id": "gmb1", "company_name": "Acme Motors", "pipeline": "gmaps",
        "status": "completed",
        "rows": [{
            "row_index": 0, "company_name": "Acme Motors", "country": "us",
            "status": "completed", "error": None,
            "result": {
                "official_website": "https://acme-motors.com",
                "gemini_cost_usd": 0.0,
                "context": {
                    "cost_breakdown": {"serpwow_request_count": 1},
                    "candidates": ["https://acme-motors.com", "https://acme-motors.co"],
                    "gmaps": {"official_website": "https://acme-motors.com"},
                    "gmaps_confidence": {"mode": "heuristic", "raw": {
                        "official_website": "https://acme-motors.com",
                        "confidence_score": 60, "confidence": "medium",
                        "reason": "name+address"}},
                },
            },
        }],
    }


class TestGmapsBatchItemsBuild(unittest.TestCase):
    """_build_batch_items_for_state (the SHARED batch engine) produces a request
    for a terminal gmaps row exactly as it would for a gsearch row."""

    def test_produces_one_item_for_completed_gmaps_row(self):
        state = _terminal_gmaps_state()
        items, row_index_by_key = legacy_app._build_batch_items_for_state(state)
        self.assertEqual(len(items), 1)
        key, request = items[0]
        self.assertEqual(key, "row-0")
        self.assertEqual(row_index_by_key, {"row-0": 0})
        # The request is the standard Gemini File-API shape (no gmaps-specific engine).
        self.assertIn("contents", request)
        prompt_text = request["contents"][0]["parts"][0]["text"]
        # The gmaps candidate URLs must be folded into the prompt.
        self.assertIn("https://acme-motors.com", prompt_text)
        self.assertIn("https://acme-motors.co", prompt_text)


class TestGmapsBatchApplyParsed(unittest.TestCase):
    """_apply_batch_parsed_to_row writes context["gemini_batch_ai"] for a gmaps
    row the same way it does for gsearch, and reporting picks it up
    ahead of the heuristic gmaps_confidence block."""

    def _sample_parsed(self):
        return {
            "official_website": "https://acme-motors.com",
            "confidence_score": 95,
            "confidence": "high",
            "reason": "LLM batch cross-check confirms name+address+domain match",
            "summary": "Acme Motors is an auto parts manufacturer.",
        }

    def test_apply_sets_gemini_batch_ai_and_completes_row(self):
        state = _terminal_gmaps_state()
        row = state["rows"][0]
        usage = {"promptTokenCount": 120, "candidatesTokenCount": 40}
        status = legacy_app._apply_batch_parsed_to_row(
            row, self._sample_parsed(), usage, "gemini-1.5-flash")

        self.assertEqual(status, "completed")
        self.assertEqual(row["status"], "completed")
        ctx = row["result"]["context"]
        self.assertIn("gemini_batch_ai", ctx)
        batch_ai = ctx["gemini_batch_ai"]
        self.assertTrue(batch_ai.get("used"))
        self.assertEqual(batch_ai["model"], "gemini-1.5-flash")
        self.assertIsInstance(batch_ai.get("raw"), dict)
        self.assertEqual(batch_ai["raw"]["official_website"], "https://acme-motors.com")
        self.assertEqual(batch_ai["raw"]["confidence_score"], 95)
        # The pre-existing heuristic block must still be present (not clobbered) --
        # gemini_batch_ai simply takes reporting priority over it.
        self.assertIn("gmaps_confidence", ctx)

    def test_summary_and_entity_results_reflect_batch_confidence(self):
        """End-to-end (offline): after applying the batch result, the shared
        reporting layer must read the BATCH confidence (95), not the
        stale heuristic confidence (60) -- proving no separate gmaps reporting
        path exists."""
        state = _terminal_gmaps_state()
        row = state["rows"][0]
        usage = {"promptTokenCount": 120, "candidatesTokenCount": 40}
        legacy_app._apply_batch_parsed_to_row(
            row, self._sample_parsed(), usage, "gemini-1.5-flash")

        results = reporting.state_to_entity_results(state)
        self.assertEqual(len(results), 1)
        entity = results[0]
        self.assertEqual(entity.website_url, "https://acme-motors.com")
        self.assertEqual(entity.confidence, 95)  # batch confidence, not heuristic's 60

        summary = reporting.build_summary(state, results)
        self.assertEqual(summary["websites_found"], 1)
        self.assertEqual(summary["websites_not_found"], 0)
        self.assertEqual(summary["model"], "gemini-1.5-flash")
        self.assertEqual(summary["token_usage"]["prompt_tokens"], 120)
        self.assertEqual(summary["token_usage"]["completion_tokens"], 40)
        self.assertEqual(summary["token_usage"]["total_tokens"], 160)


if __name__ == "__main__":
    unittest.main()
