import asyncio
import unittest
from unittest import mock

from app.services.serpwow import legacy_app


def _state():
    return {"upload_id": "gm1", "company_name": "Acme", "pipeline": "gmaps",
            "status": "completed",
            "rows": [{"row_index": 0, "company_name": "Acme", "country": "us", "status": "completed", "error": None,
                      "result": {"official_website": "https://acme.com", "gemini_cost_usd": 0.0,
                                 "context": {"cost_breakdown": {"serpwow_request_count": 2},
                                             "gmaps_confidence": {"raw": {
                                                 "official_website": "https://acme.com",
                                                 "confidence_score": 90}}}}}]}


class TestGmapsStatusBlock(unittest.TestCase):
    def test_status_has_serpwow_summary_for_gmaps(self):
        with mock.patch.object(legacy_app, "get_upload_state",
                               new=mock.AsyncMock(return_value=_state())), \
             mock.patch.object(legacy_app, "maybe_reconcile_gemini_batch_status",
                               new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
             mock.patch.object(legacy_app, "maybe_fail_stale_processing_rows",
                               new=mock.AsyncMock(side_effect=lambda _id, s: s)):
            resp = asyncio.run(legacy_app.upload_status("gm1"))
        self.assertIsNotNone(resp["serpwow_summary"])
        self.assertEqual(resp["serpwow_summary"]["websites_found"], 1)
        self.assertIsNone(resp["serpwow_summary"]["model"])  # no LLM in gmaps
