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
        # gmaps left this set when it moved to the S3-only runner: it has no state.json,
        # so it writes the same four files from gmaps_outputs by streaming instead.
        self.assertEqual(REPORTING_PIPELINES, {PIPELINE_GSEARCH, PIPELINE_RELATIONSHIP})
        self.assertNotIn(PIPELINE_GMAPS, REPORTING_PIPELINES)

    def test_result_file_allowlist_has_skipped_csv(self):
        self.assertIn("skipped.csv", engine._GSEARCH_RESULT_FILES)

    def test_file_links_advertises_relationship_files(self):
        with patch.dict("os.environ", {"S3_BUCKET": "b"}):
            links = engine._upload_file_links("up1", "Acme", "relationship")
        for name in ("confirmed_relation.csv", "notconfirmed_relation.csv",
                     "report.json", "run.log"):
            self.assertIn(name, links)
        self.assertNotIn("found.csv", links)
        self.assertNotIn("skipped.csv", links)

    def test_update_supabase_run_gsearch_state_unchanged(self):
        state = {"upload_id": "g1", "company_name": "Acme", "pipeline": "gsearch",
                  "status": "completed", "run_db_id": "run-db-2",
                  "success_rows": 1, "failed_rows": 1, "processing_seconds_total": 1.0,
                  "rows": [
                      {"company_name": "A", "country": "us", "status": "completed", "error": None,
                       "outcome": "found",
                       "result": {"official_website": "https://a.com",
                                  "context": {"cost_breakdown": {"serpwow_request_count": 1}}}},
                      {"company_name": "B", "country": "us", "status": "failed", "error": "x",
                       "outcome": "error",
                       "result": {"official_website": None,
                                  "context": {"cost_breakdown": {"serpwow_request_count": 1}}}},
                  ]}

        class _FakeCompanySvc:
            def __init__(self):
                self.kwargs = None

            def update_run(self, run_db_id, **fields):
                self.kwargs = fields
                return True

        svc = _FakeCompanySvc()
        with patch("app.services.companies.get_company_service", return_value=svc), \
             patch.object(engine, "_upload_file_links", return_value={}):
            engine._update_supabase_run(state)
        # success/failed follow the found/errored outcome_breakdown, not a
        # pass-through of state's success_rows/failed_rows.
        self.assertEqual(svc.kwargs["success_count"], 1)
        self.assertEqual(svc.kwargs["failed_count"], 1)
        self.assertNotIn("total_rows", svc.kwargs)


if __name__ == "__main__":
    unittest.main()
