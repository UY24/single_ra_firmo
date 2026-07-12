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
    def tearDown(self):
        getattr(legacy_app, "_s3_run_prefix_cache", {}).clear()

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
            resolve = mock.Mock()
            with mock.patch.object(legacy_app, "get_upload_state",
                                   new=mock.AsyncMock(return_value=_state())), \
                 mock.patch.object(legacy_app, "maybe_reconcile_gemini_batch_status",
                                   new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
                 mock.patch.object(legacy_app, "maybe_fail_stale_processing_rows",
                                   new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
                 mock.patch.object(legacy_app, "_find_upload_dir", return_value=run_dir), \
                 mock.patch.object(legacy_app, "get_s3_client", return_value=s3), \
                 mock.patch.object(legacy_app, "_find_s3_upload_key_sync", resolve), \
                 mock.patch.dict("os.environ", {"S3_BUCKET": "bucket"}):
                resp = asyncio.run(legacy_app.upload_status("gm1"))
        self.assertEqual(resp["serpwow_summary"]["available_files"],
                         ["found.csv", "notFound.csv", "report.json", "run.log"])
        s3.list_objects_v2.assert_not_called()
        resolve.assert_not_called()

    def test_normal_s3_write_seeds_exact_run_prefix_before_background_write(self):
        scheduled = []

        def capture_task(coro):
            scheduled.append(coro)
            coro.close()
            return mock.Mock()

        state = {"company_name": "Acme Inc", "pipeline": "gmaps"}
        with mock.patch.object(legacy_app, "_state_file",
                               return_value=Path("/tmp/unused-state.json")), \
             mock.patch.object(legacy_app, "_write_json"), \
             mock.patch.object(legacy_app.asyncio, "create_task",
                               side_effect=capture_task), \
             mock.patch.dict("os.environ", {"S3_BUCKET": "bucket"}):
            asyncio.run(legacy_app.write_upload_artifact("up-1", "state", state))

        self.assertEqual(legacy_app._s3_run_prefix_cache.get("up-1"),
                         "acme-inc/gmaps/up-1")
        self.assertEqual(len(scheduled), 1)

    def test_normal_s3_write_retains_resolved_legacy_run_prefix(self):
        scheduled = []

        def capture_task(coro):
            scheduled.append(coro)
            coro.close()
            return mock.Mock()

        legacy_app._s3_run_prefix_cache["gm1"] = "ISI_Market_Test/gmaps/gm1"
        state = {"company_name": "ISI Market Test", "pipeline": "gmaps"}
        with mock.patch.object(legacy_app, "_state_file",
                               return_value=Path("/tmp/unused-state.json")), \
             mock.patch.object(legacy_app, "_write_json"), \
             mock.patch.object(legacy_app.asyncio, "create_task",
                               side_effect=capture_task), \
             mock.patch.dict("os.environ", {"S3_BUCKET": "bucket"}):
            asyncio.run(legacy_app.write_upload_artifact("gm1", "state", state))

        self.assertEqual(legacy_app._s3_run_prefix_cache["gm1"],
                         "ISI_Market_Test/gmaps/gm1")
        self.assertEqual(len(scheduled), 1)

    def test_cached_current_prefix_lists_once_without_suffix_scan(self):
        class FakeS3:
            def __init__(self):
                self.calls = []

            def list_objects_v2(self, **kwargs):
                self.calls.append(kwargs)
                return {"Contents": [{"Key": "acme/gmaps/gm1/run.log"}]}

        s3 = FakeS3()
        resolve = mock.Mock()
        legacy_app._s3_run_prefix_cache["gm1"] = "acme/gmaps/gm1"
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(legacy_app, "_find_upload_dir", return_value=Path(td)), \
             mock.patch.object(legacy_app, "get_s3_client", return_value=s3), \
             mock.patch.object(legacy_app, "_find_s3_upload_key_sync", resolve), \
             mock.patch.dict("os.environ", {"S3_BUCKET": "bucket"}):
            available = asyncio.run(legacy_app._available_reporting_files(
                "gm1", "Acme", "gmaps"))

        self.assertEqual(available, ["run.log"])
        self.assertEqual(len(s3.calls), 1)
        self.assertEqual(s3.calls[0]["Prefix"], "acme/gmaps/gm1/")
        resolve.assert_not_called()

    def test_reading_legacy_s3_state_caches_actual_run_prefix(self):
        legacy_key = "ISI_Market_Test/gmaps/gm1/state.json"
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(legacy_app, "_state_file", return_value=Path(td) / "state.json"), \
             mock.patch.object(legacy_app, "_find_s3_upload_key_sync", return_value=legacy_key), \
             mock.patch.object(legacy_app, "_read_json_from_s3_sync", return_value={"upload_id": "gm1"}), \
             mock.patch.dict("os.environ", {"S3_BUCKET": "bucket"}):
            asyncio.run(legacy_app.read_upload_artifact("gm1", "state"))
        self.assertEqual(legacy_app._s3_run_prefix_cache["gm1"],
                         "ISI_Market_Test/gmaps/gm1")

    def test_legacy_s3_prefix_resolution_is_cached_after_first_availability_check(self):
        class LegacyS3:
            def __init__(self):
                self.calls = []

            def list_objects_v2(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs["Prefix"] == "ISI_Market_Test/gmaps/gm1/":
                    return {"Contents": [
                        {"Key": "ISI_Market_Test/gmaps/gm1/found.csv"},
                        {"Key": "ISI_Market_Test/gmaps/gm1/run.log"},
                    ]}
                return {"Contents": []}

        s3 = LegacyS3()
        resolve = mock.Mock(return_value="ISI_Market_Test/gmaps/gm1/state.json")
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(legacy_app, "_find_upload_dir", return_value=Path(td)), \
             mock.patch.object(legacy_app, "get_s3_client", return_value=s3), \
             mock.patch.object(legacy_app, "_find_s3_upload_key_sync", resolve), \
             mock.patch.dict("os.environ", {"S3_BUCKET": "bucket"}):
            first = asyncio.run(legacy_app._available_reporting_files(
                "gm1", "ISI Market Test", "gmaps"))
            first_calls = list(s3.calls)
            second = asyncio.run(legacy_app._available_reporting_files(
                "gm1", "ISI Market Test", "gmaps"))

        self.assertEqual(first, ["found.csv", "run.log"])
        self.assertEqual(second, first)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual([call["Prefix"] for call in first_calls], [
            "isi-market-test/gmaps/gm1/",
            "ISI_Market_Test/gmaps/gm1/",
        ])
        self.assertEqual(len(s3.calls), 3)
        self.assertEqual(s3.calls[-1]["Prefix"], "ISI_Market_Test/gmaps/gm1/")
        self.assertTrue(all(call["Delimiter"] == "/" for call in s3.calls))
