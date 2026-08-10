"""Supabase + file links for the STATE-DRIVEN reporting path.

Fixtures say gsearch, not gmaps: gmaps left this path in 2026-08 for the S3-only runner,
whose terminal write is s3_run_driver.notify_terminal (see test_gmaps_runner).
"""
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
    return {"upload_id": "gm1", "company_name": "Acme", "pipeline": "gsearch",
            "status": "completed_with_errors", "run_db_id": "abc",
            "success_rows": 1, "failed_rows": 1, "processing_seconds_total": 2.0,
            "rows": [
                {"company_name": "Acme", "country": "us", "status": "completed", "error": None,
                 "result": {"official_website": "https://acme.com", "gemini_cost_usd": 0.0,
                            "context": {"cost_breakdown": {"serpwow_request_count": 3},
                                        "gmaps_confidence": {"raw": {
                                            "official_website": "https://acme.com",
                                            "confidence_score": 90}}}}},
                {"company_name": "NoWeb", "country": "us", "status": "failed", "error": "x",
                 "result": {"official_website": None,
                            "context": {"cost_breakdown": {"serpwow_request_count": 2}}}}]}


class TestSerpwowSupabaseAndLinks(unittest.TestCase):
    def test_terminal_update_includes_counts_and_zero_llm_cost(self):
        svc = _FakeSvc()
        with mock.patch("app.services.companies.get_company_service", return_value=svc), \
             mock.patch.object(legacy_app, "_upload_file_links", return_value={"report.json": "s3://x"}):
            ok = legacy_app._update_supabase_run(_state())
        self.assertTrue(ok)
        self.assertEqual(svc.kwargs["websites_found"], 1)
        self.assertEqual(svc.kwargs["websites_not_found"], 1)
        self.assertEqual(svc.kwargs["cost"]["serpwow_searches"], 5)
        self.assertEqual(svc.kwargs["cost"]["llm_usd"], 0.0)
        self.assertEqual(svc.kwargs["token_usage"]["prompt_tokens"], 0)

    def test_file_links_advertise_result_files(self):
        with mock.patch.dict("os.environ", {"S3_BUCKET": "bkt"}):
            links = legacy_app._upload_file_links("gm1", "Acme", "gsearch")
        for name in ("found.csv", "notFound.csv", "report.json", "run.log"):
            self.assertIn(name, links)
