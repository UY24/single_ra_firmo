# backend/tests/test_relationship_gates.py
import unittest
from unittest.mock import patch

from app.services.serpwow import engine
from app.services.serpwow.constants import (
    PIPELINE_GMAPS,
    PIPELINE_GSEARCH,
    PIPELINE_RELATIONSHIP,
    REPORTING_PIPELINES,
)


class TestRelationshipGates(unittest.TestCase):
    def test_pipeline_constant(self):
        self.assertEqual(PIPELINE_RELATIONSHIP, "relationship")
        self.assertEqual(
            REPORTING_PIPELINES,
            {PIPELINE_GSEARCH, PIPELINE_GMAPS, PIPELINE_RELATIONSHIP},
        )

    def test_batch_gate_follows_env(self):
        with patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            self.assertFalse(engine._batch_postprocess_enabled_for("relationship"))
        with patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "true"}):
            self.assertTrue(engine._batch_postprocess_enabled_for("relationship"))

    def test_batch_pending_defers_relationship(self):
        state = {"pipeline": "relationship",
                 "gemini_batch": {"status": "running"}}
        with patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "true"}):
            self.assertTrue(engine._batch_postprocess_pending(state))
        with patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            self.assertFalse(engine._batch_postprocess_pending(state))

    def test_result_file_allowlist_has_skipped_csv(self):
        self.assertIn("skipped.csv", engine._GSEARCH_RESULT_FILES)

    def test_file_links_advertises_relationship_files(self):
        with patch.dict("os.environ", {"S3_BUCKET": "b"}):
            links = engine._upload_file_links("up1", "Acme", "relationship")
        for name in ("found.csv", "notFound.csv", "skipped.csv", "report.json", "run.log"):
            self.assertIn(name, links)

    def test_status_summary_passthrough_keys(self):
        # the /status endpoint copies these keys from build_summary when present
        from app.services.serpwow import serpwow_reporting
        state = {"upload_id": "u", "pipeline": "relationship", "status": "completed",
                 "relationship": {"header": [], "original_rows": [{}], "blank_row_indices": [],
                                  "blank_rows": 0, "row_count_original": 1},
                 "rows": []}
        summ = serpwow_reporting.build_summary(state, [])
        for key in ("blank_rows", "searchable_rows", "unique_pairs", "relationship_breakdown"):
            self.assertIn(key, summ)


if __name__ == "__main__":
    unittest.main()
