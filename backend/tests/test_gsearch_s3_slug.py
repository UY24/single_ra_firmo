"""
Task 7: One S3 folder per run (per-row slug fix).

Asserts that:
1. _upload_s3_prefix produces the correct prefix for an upload company name.
2. _build_row_job_payload carries upload_company_name in the payload.
3. process_upload_job passes the upload company (not the row company) to
   upload_serpwow_json_to_s3 as the company_name kwarg.
"""
import asyncio
import unittest
from unittest import mock

from app.services.serpwow import legacy_app as app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fake_gsearch(company_name, country, firm_id=None, input_industry=None,
                        input_full_address=None, debug_upload_id=None, debug_row_index=None,
                        phase="all"):
    """Minimal async stub for execute_gsearch_lookup_for_worker."""
    from app.services.serpwow.legacy_app import CrawlResponse

    resp = CrawlResponse(
        official_website="https://row-company.com",
        company_name=company_name,
        country=country,
        summary="stub",
        massive_proxy_cost_usd=0.0,
        serpwow_cost_usd=0.0,
        gemini_cost_usd=0.0,
        total_cost_usd=0.0,
        context={
            "final_url_selection_ai": {"used": False, "raw": None},
            "candidates": [],
        },
    )
    raw_json = '{"stub": true}'
    return resp, raw_json


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestS3SlugPrefix(unittest.TestCase):
    """Unit test: _upload_s3_prefix uses the upload company slug."""

    def test_per_row_key_uses_upload_company(self):
        prefix = app._upload_s3_prefix("uid123", "ISI Market Test", "gsearch")
        self.assertIn("isi-market-test/gsearch/uid123", prefix)


class TestBuildRowJobPayload(unittest.TestCase):
    """Unit test: _build_row_job_payload carries upload_company_name."""

    def _make_row(self):
        return {
            "row_index": 0,
            "company_name": "Row Company LLC",
            "country": "US",
            "firm_id": "",
            "industry": "",
            "full_address": "",
            "official_website": "",
        }

    def test_payload_includes_upload_company_name(self):
        row = self._make_row()
        payload = app._build_row_job_payload(
            "up-001", row, "gsearch", upload_company_name="ISI Market Test"
        )
        self.assertIn("upload_company_name", payload)
        self.assertEqual(payload["upload_company_name"], "ISI Market Test")

    def test_payload_defaults_upload_company_name_to_empty(self):
        row = self._make_row()
        payload = app._build_row_job_payload("up-001", row, "gsearch")
        # Default must be present (empty string is fine)
        self.assertIn("upload_company_name", payload)
        self.assertEqual(payload["upload_company_name"], "")


class TestProcessUploadJobUsesUploadCompany(unittest.TestCase):
    """
    Behavioral test: process_upload_job passes the upload company (from
    job['upload_company_name']) to upload_serpwow_json_to_s3, not the row company.
    """

    def _make_job(self, upload_company="ISI Market Test", row_company="Row Company LLC"):
        return {
            "upload_id": "test-upload-id",
            "row_index": 0,
            "company_name": row_company,
            "country": "US",
            "firm_id": "",
            "industry": "",
            "full_address": "",
            "official_website": "",
            "pipeline": "gsearch",
            "phase": "all",
            "uploaded_at": "2024-01-01T00:00:00Z",
            "upload_company_name": upload_company,
        }

    def test_s3_upload_uses_upload_company_not_row_company(self):
        job = self._make_job(upload_company="ISI Market Test", row_company="Row Company LLC")

        captured = {}

        async def fake_s3_upload(upload_id, row_index, company_name, raw_json, pipeline=""):
            captured["company_name"] = company_name
            return "s3://bucket/key", None

        async def fake_update_row_state(upload_id, row_index, **kwargs):
            pass

        async def fake_get_upload_state(upload_id):
            return {
                "rows": [{
                    "row_index": 0,
                    "company_name": "Row Company LLC",
                    "status": "queued",
                }]
            }

        async def _fake_persist(uid, s):
            pass

        with mock.patch.object(app, "execute_gsearch_lookup_for_worker",
                               side_effect=_fake_gsearch), \
             mock.patch.object(app, "upload_serpwow_json_to_s3",
                               side_effect=fake_s3_upload), \
             mock.patch.object(app, "update_row_state",
                               side_effect=fake_update_row_state), \
             mock.patch.object(app, "get_upload_state",
                               side_effect=fake_get_upload_state), \
             mock.patch.object(app, "persist_upload_state",
                               side_effect=_fake_persist), \
             mock.patch.dict("os.environ", {
                 "GSEARCH_LLM_BATCH": "false",
                 "ENABLE_FINAL_URL_GEMINI": "false",
             }):
            asyncio.run(app.process_upload_job(job))

        self.assertIn("company_name", captured,
                      "upload_serpwow_json_to_s3 was never called")
        self.assertEqual(
            captured["company_name"],
            "ISI Market Test",
            f"Expected upload company 'ISI Market Test', got {captured['company_name']!r}. "
            "process_upload_job is still using the row company for S3 keying.",
        )


if __name__ == "__main__":
    unittest.main()
