import unittest
from unittest import mock

from app.services.serpwow import engine as legacy_app


class _FakeSvc:
    def __init__(self):
        self.kwargs = None
    def update_run(self, run_db_id, **fields):
        self.kwargs = fields
        return True


def _state():
    return {"upload_id": "up1", "company_name": "Acme", "pipeline": "gsearch",
            "status": "completed", "run_db_id": "abc", "success_rows": 1, "failed_rows": 1,
            "processing_seconds_total": 2.0,
            "rows": [
                {"company_name": "Acme", "country": "us", "status": "completed", "error": None,
                 "result": {"official_website": "https://acme.com", "gemini_cost_usd": 0.0002,
                            "context": {"cost_breakdown": {"serpwow_request_count": 3},
                                        "final_url_selection_ai": {"usage": {
                                            "promptTokenCount": 40, "candidatesTokenCount": 8},
                                            "raw": {"official_website": "https://acme.com",
                                                    "confidence_score": 80}}}}},
                {"company_name": "NoWeb", "country": "us", "status": "failed", "error": "x",
                 "result": {"official_website": None,
                            "context": {"cost_breakdown": {"serpwow_request_count": 5}}}}]}


def _state_three_way_outcomes():
    """1 found / 1 not_found (business, no error) / 1 errored (real failure).

    This synthetic state omits the legacy success_rows/failed_rows keys, so the
    pre-Task-8 code left success_count/failed_count at None for the gsearch path
    (the old code only overrode them inside the relationship branch). The new
    mapping must report success_count==1 (found) and failed_count==1 (errored
    only), driven off summary["outcome_breakdown"]."""
    return {"upload_id": "up2", "company_name": "Acme", "pipeline": "gsearch",
            "status": "completed_with_errors", "run_db_id": "abc2",
            "processing_seconds_total": 3.0,
            "rows": [
                {"company_name": "Found", "country": "us", "status": "completed", "error": None,
                 "outcome": "found",
                 "result": {"official_website": "https://found.com",
                            "context": {"cost_breakdown": {"serpwow_request_count": 1}}}},
                {"company_name": "NotFound", "country": "us", "status": "completed",
                 "error": "Official website not found after Gemini batch post-processing.",
                 "outcome": "not_found",
                 "result": {"official_website": None,
                            "context": {"cost_breakdown": {"serpwow_request_count": 1}}}},
                {"company_name": "Errored", "country": "us", "status": "failed",
                 "error": "SerpWow request timed out", "outcome": "error",
                 "error_source": "serpwow", "error_category": "timeout",
                 "result": {"official_website": None,
                            "context": {"cost_breakdown": {"serpwow_request_count": 1}}}},
            ]}


class TestGsearchSupabase(unittest.TestCase):
    def test_terminal_update_includes_found_cost_tokens(self):
        svc = _FakeSvc()
        with mock.patch("app.services.companies.get_company_service", return_value=svc), \
             mock.patch.object(legacy_app, "_upload_file_links", return_value={"report.json": "s3://x"}):
            ok = legacy_app._update_supabase_run(_state())
        self.assertTrue(ok)
        self.assertEqual(svc.kwargs["websites_found"], 1)
        self.assertEqual(svc.kwargs["websites_not_found"], 1)
        self.assertEqual(svc.kwargs["cost"]["serpwow_searches"], 8)
        self.assertEqual(svc.kwargs["token_usage"]["prompt_tokens"], 40)

    def test_success_count_is_found_failed_count_is_errored_only(self):
        svc = _FakeSvc()
        with mock.patch("app.services.companies.get_company_service", return_value=svc), \
             mock.patch.object(legacy_app, "_upload_file_links", return_value={}):
            ok = legacy_app._update_supabase_run(_state_three_way_outcomes())
        self.assertTrue(ok)
        self.assertEqual(svc.kwargs["success_count"], 1)
        self.assertEqual(svc.kwargs["failed_count"], 1)  # NOT 2 (not_found + error)
