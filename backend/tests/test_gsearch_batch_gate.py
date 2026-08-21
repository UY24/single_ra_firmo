import unittest
from unittest import mock

from app.services.serpwow import engine as legacy_app


class TestBatchGate(unittest.TestCase):
    def test_gsearch_uses_gsearch_flag(self):
        with mock.patch.dict("os.environ", {"LLM_BATCH": "true"}):
            self.assertTrue(legacy_app._batch_postprocess_enabled_for("gsearch"))
            self.assertFalse(legacy_app._batch_postprocess_enabled_for("gmaps"))

    def test_other_pipeline_never_enabled(self):
        with mock.patch.dict("os.environ", {"LLM_BATCH": "true"}):
            self.assertFalse(legacy_app._batch_postprocess_enabled_for("gmaps"))

    def test_relationship_never_uses_the_shared_row_batch_engine(self):
        """relationship drives its own Gemini Batch in relationship_runner; the old
        RELATIONSHIP_LLM_BATCH toggle on this shared gate is gone."""
        with mock.patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "true"}):
            self.assertFalse(legacy_app._batch_postprocess_enabled_for("relationship"))
            self.assertFalse(legacy_app._batch_postprocess_pending(
                {"pipeline": "relationship", "gemini_batch": {"status": "running"}}))


def _gsearch_row(candidates=(), row_index=1, skip_llm=False, status="failed"):
    return {
        "row_index": row_index, "company_name": "Acme", "country": "US",
        "status": status, "error": "SerpWow failed (HTTP 503).",
        "result": {"official_website": None, "context": {
            "pipeline": "gsearch", "candidates": list(candidates),
            "skip_llm": skip_llm,
            "formatted_results": [{"success": False, "status_code": 503}],
        }},
    }


class TestBatchItemSeeding(unittest.TestCase):
    """_build_batch_items_for_state decides which terminal rows the shared Gemini
    batch actually sees."""

    def test_skip_llm_rows_are_not_seeded(self):
        state = {"rows": [_gsearch_row(candidates=("https://acme.com/",), row_index=1),
                          _gsearch_row(candidates=("https://acme.com/",), row_index=2,
                                       skip_llm=True)]}
        items, by_key = legacy_app._build_batch_items_for_state(state)
        self.assertEqual([k for k, _ in items], ["row-1"])
        self.assertEqual(by_key, {"row-1": 1})

    def test_gsearch_row_without_candidates_is_not_seeded(self):
        items, by_key = legacy_app._build_batch_items_for_state(
            {"rows": [_gsearch_row()]})
        self.assertEqual(items, [])
        self.assertEqual(by_key, {})

    def test_gsearch_row_with_candidates_remains_eligible(self):
        items, by_key = legacy_app._build_batch_items_for_state({
            "rows": [_gsearch_row(candidates=("https://acme.com/",))]})
        self.assertEqual([key for key, _ in items], ["row-1"])
        self.assertEqual(by_key, {"row-1": 1})

    def test_the_no_candidates_skip_is_gsearch_only(self):
        """The guard is `pipeline == gsearch AND not candidates` — a defence for legacy
        gsearch rows written before skip_llm existed. Widening it to every pipeline would
        silently drop candidate-less gmaps rows from the batch, so pin gmaps here."""
        row = _gsearch_row()
        row["result"]["context"]["pipeline"] = "gmaps"
        items, by_key = legacy_app._build_batch_items_for_state({"rows": [row]})
        self.assertEqual([key for key, _ in items], ["row-1"])
        self.assertEqual(by_key, {"row-1": 1})


if __name__ == "__main__":
    unittest.main()
