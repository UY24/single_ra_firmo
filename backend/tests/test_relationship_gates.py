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


class _FakeCompanySvc:
    def __init__(self):
        self.kwargs = None

    def update_run(self, run_db_id, **fields):
        self.kwargs = fields
        return True


def _relationship_state_one_pair_two_sources():
    """1 pair (found) fanning out to 2 original CSV rows — pair-level counters
    (success_rows/failed_rows/total_rows=1) must NOT leak into Supabase/Slack;
    original-row-level counts (2 found) must be used instead."""
    return {
        "upload_id": "u1", "company_name": "Acme", "pipeline": "relationship",
        "status": "completed", "run_db_id": "run-db-1",
        "total_rows": 1, "success_rows": 1, "failed_rows": 0,
        "processing_seconds_total": 3.0,
        "relationship": {
            "header": ["Company_Name_X", "Company_Name_Y"],
            "original_rows": [{"Company_Name_X": "eastlinkcap", "Company_Name_Y": "Modal"},
                               {"Company_Name_X": "eastlinkcap", "Company_Name_Y": "Modal"}],
            "blank_row_indices": [], "blank_rows": 0, "row_count_original": 2,
        },
        "rows": [
            {"row_index": 1, "company_name": "Modal", "country": "",
             "x_name": "eastlinkcap", "input_url": "", "city": "",
             "source_row_indices": [0, 1],
             "status": "completed", "error": None,
             "result": {"official_website": "https://modal.com/",
                        "gemini_cost_usd": 0.001,
                        "context": {"pipeline": "relationship",
                                    "candidates": ["https://modal.com/"],
                                    "relationship": {"status": "confirmed",
                                                      "summary": "s", "flags": []},
                                    "cost_breakdown": {"serpwow_request_count": 4}}},
             },
        ],
    }


class TestRelationshipOriginalRowLevelCounts(unittest.TestCase):
    """Finding 2: relationship state counters are pair-level but Supabase/Slack
    must report original-row-level (fan-out) counts, matching found.csv/notFound.csv."""

    def test_update_supabase_run_uses_original_row_counts(self):
        state = _relationship_state_one_pair_two_sources()
        svc = _FakeCompanySvc()
        with patch("app.services.companies.get_company_service", return_value=svc), \
             patch.object(engine, "_upload_file_links", return_value={}):
            ok = engine._update_supabase_run(state)
        self.assertTrue(ok)
        # 1 pair, but 2 original rows fanned out -> Supabase must see 2, not 1.
        self.assertEqual(svc.kwargs["websites_found"], 2)
        self.assertEqual(svc.kwargs["websites_not_found"], 0)
        self.assertEqual(svc.kwargs["success_count"], 2)
        self.assertEqual(svc.kwargs["failed_count"], 0)
        self.assertEqual(svc.kwargs["total_rows"], 2)

    def test_update_supabase_run_gsearch_state_unchanged(self):
        state = {"upload_id": "g1", "company_name": "Acme", "pipeline": "gsearch",
                  "status": "completed", "run_db_id": "run-db-2",
                  "success_rows": 1, "failed_rows": 1, "processing_seconds_total": 1.0,
                  "rows": [
                      {"company_name": "A", "country": "us", "status": "completed", "error": None,
                       "result": {"official_website": "https://a.com",
                                  "context": {"cost_breakdown": {"serpwow_request_count": 1}}}},
                      {"company_name": "B", "country": "us", "status": "failed", "error": "x",
                       "result": {"official_website": None,
                                  "context": {"cost_breakdown": {"serpwow_request_count": 1}}}},
                  ]}
        svc = _FakeCompanySvc()
        with patch("app.services.companies.get_company_service", return_value=svc), \
             patch.object(engine, "_upload_file_links", return_value={}):
            engine._update_supabase_run(state)
        # gsearch has no "relationship" block -> untouched pass-through counters.
        self.assertEqual(svc.kwargs["success_count"], 1)
        self.assertEqual(svc.kwargs["failed_count"], 1)
        self.assertNotIn("total_rows", svc.kwargs)

    def test_notify_slack_terminal_uses_original_row_counts(self):
        state = _relationship_state_one_pair_two_sources()
        with patch("app.core.notify.notify_run_complete") as notify_complete:
            engine._notify_slack_terminal(state)
        notify_complete.assert_called_once()
        kwargs = notify_complete.call_args.kwargs
        self.assertEqual(kwargs["success"], 2)
        self.assertEqual(kwargs["failed"], 0)
        self.assertEqual(kwargs["total_rows"], 2)


if __name__ == "__main__":
    unittest.main()
