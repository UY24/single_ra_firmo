import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.serpwow import engine as legacy_app


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
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            (run_dir / "found.csv").write_text("company\nAcme\n")
            (run_dir / "report.json").write_text("{}")
            with mock.patch.object(legacy_app, "get_upload_state",
                                   new=mock.AsyncMock(return_value=_state())), \
                 mock.patch.object(legacy_app, "maybe_reconcile_gemini_batch_status",
                                   new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
                 mock.patch.object(legacy_app, "maybe_fail_stale_processing_rows",
                                   new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
                 mock.patch.object(legacy_app, "_find_upload_dir", return_value=run_dir), \
                 mock.patch.dict("os.environ", {"S3_BUCKET": ""}, clear=False):
                resp = asyncio.run(legacy_app.upload_status("gm1"))
        self.assertIsNotNone(resp["serpwow_summary"])
        self.assertEqual(resp["serpwow_summary"]["websites_found"], 1)
        self.assertIsNone(resp["serpwow_summary"]["model"])  # no LLM in gmaps
        self.assertEqual(resp["serpwow_summary"]["outcome_breakdown"],
                         {"found": 1, "not_found": 0, "errored": 0})
        self.assertEqual(resp["serpwow_summary"]["error_breakdown"],
                         {"by_source": {}, "by_category": {}})
        self.assertEqual(resp["serpwow_summary"]["available_files"],
                         ["found.csv", "report.json"])

    def test_status_checks_s3_for_missing_reporting_files(self):
        class FakeS3:
            def __init__(self):
                self.calls = []

            def list_objects_v2(self, **kwargs):
                self.calls.append(kwargs)
                return {"Contents": [
                    {"Key": "acme/gmaps/gm1/run.log"},
                    {"Key": "acme/gmaps/gm1/unrelated.txt"},
                ],
                    "CommonPrefixes": [
                        {"Prefix": "acme/gmaps/gm1/errors/"},
                        {"Prefix": "acme/gmaps/gm1/serpwow_response/"},
                    ],
                    "IsTruncated": True,
                }

        s3 = FakeS3()
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(legacy_app, "get_upload_state",
                               new=mock.AsyncMock(return_value=_state())), \
             mock.patch.object(legacy_app, "maybe_reconcile_gemini_batch_status",
                               new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
             mock.patch.object(legacy_app, "maybe_fail_stale_processing_rows",
                               new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
             mock.patch.object(legacy_app, "_find_upload_dir", return_value=Path(td)), \
             mock.patch.object(legacy_app, "get_s3_client", return_value=s3), \
             mock.patch.dict("os.environ", {"S3_BUCKET": "bucket"}):
            resp = asyncio.run(legacy_app.upload_status("gm1"))
        self.assertEqual(resp["serpwow_summary"]["available_files"], ["run.log"])
        self.assertEqual(len(s3.calls), 1)
        self.assertEqual(s3.calls[0]["Bucket"], "bucket")
        self.assertEqual(s3.calls[0]["Prefix"], "acme/gmaps/gm1/")
        self.assertEqual(s3.calls[0]["Delimiter"], "/")

    def test_status_skips_s3_when_all_reporting_files_are_local(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            for name in ("found.csv", "notFound.csv", "report.json", "run.log"):
                (run_dir / name).write_text("")
            s3 = mock.Mock()
            with mock.patch.object(legacy_app, "get_upload_state",
                                   new=mock.AsyncMock(return_value=_state())), \
                 mock.patch.object(legacy_app, "maybe_reconcile_gemini_batch_status",
                                   new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
                 mock.patch.object(legacy_app, "maybe_fail_stale_processing_rows",
                                   new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
                 mock.patch.object(legacy_app, "_find_upload_dir", return_value=run_dir), \
                 mock.patch.object(legacy_app, "get_s3_client", return_value=s3), \
                 mock.patch.dict("os.environ", {"S3_BUCKET": "bucket"}):
                resp = asyncio.run(legacy_app.upload_status("gm1"))
        self.assertEqual(resp["serpwow_summary"]["available_files"],
                         ["found.csv", "notFound.csv", "report.json", "run.log"])
        s3.list_objects_v2.assert_not_called()
