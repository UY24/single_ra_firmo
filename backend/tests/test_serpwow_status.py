"""Status block + S3 run-prefix resolution for the STATE-DRIVEN pipelines.

Fixtures say gsearch: gmaps left this path in 2026-08 for the S3-only runner, whose
status comes from status.json counters (see test_gmaps_runner).
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.serpwow import engine as legacy_app


def _state():
    return {"upload_id": "gm1", "company_name": "Acme", "pipeline": "gsearch",
            "status": "completed",
            "rows": [{"row_index": 0, "company_name": "Acme", "country": "us", "status": "completed", "error": None,
                      "result": {"official_website": "https://acme.com", "gemini_cost_usd": 0.0,
                                 "context": {"cost_breakdown": {"serpwow_request_count": 2},
                                             "gmaps_confidence": {"raw": {
                                                 "official_website": "https://acme.com",
                                                 "confidence_score": 90}}}}}]}


class TestSerpwowStatusBlock(unittest.TestCase):
    def tearDown(self):
        getattr(legacy_app, "_s3_run_prefix_cache", {}).clear()

    def test_status_has_serpwow_summary(self):
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

    def test_running_status_does_not_scan_reporting_files(self):
        state = _state()
        state["status"] = "running"
        state["rows"][0]["status"] = "processing"
        available = mock.AsyncMock(return_value=["run.log"])
        with mock.patch.object(legacy_app, "get_upload_state",
                               new=mock.AsyncMock(return_value=state)), \
             mock.patch.object(legacy_app, "maybe_reconcile_gemini_batch_status",
                               new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
             mock.patch.object(legacy_app, "maybe_fail_stale_processing_rows",
                               new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
             mock.patch.object(legacy_app, "_available_reporting_files", new=available):
            resp = asyncio.run(legacy_app.upload_status("gm1"))
        available.assert_not_awaited()
        self.assertEqual(resp["serpwow_summary"]["available_files"], [])

    def test_terminal_rows_with_running_batch_do_not_scan_reporting_files(self):
        state = _state()
        state["gemini_batch"] = {"status": "running"}
        available = mock.AsyncMock(return_value=["run.log"])
        with mock.patch.object(legacy_app, "get_upload_state",
                               new=mock.AsyncMock(return_value=state)), \
             mock.patch.object(legacy_app, "maybe_reconcile_gemini_batch_status",
                               new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
             mock.patch.object(legacy_app, "maybe_fail_stale_processing_rows",
                               new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
             mock.patch.object(legacy_app, "_available_reporting_files", new=available):
            resp = asyncio.run(legacy_app.upload_status("gm1"))
        available.assert_not_awaited()
        self.assertEqual(resp["serpwow_summary"]["available_files"], [])

    def test_terminal_status_with_settled_batch_scans_reporting_files(self):
        state = _state()
        state["gemini_batch"] = {"status": "succeeded"}
        available = mock.AsyncMock(return_value=["run.log"])
        with mock.patch.object(legacy_app, "get_upload_state",
                               new=mock.AsyncMock(return_value=state)), \
             mock.patch.object(legacy_app, "maybe_reconcile_gemini_batch_status",
                               new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
             mock.patch.object(legacy_app, "maybe_fail_stale_processing_rows",
                               new=mock.AsyncMock(side_effect=lambda _id, s: s)), \
             mock.patch.object(legacy_app, "_available_reporting_files", new=available):
            resp = asyncio.run(legacy_app.upload_status("gm1"))
        available.assert_awaited_once_with("gm1", "Acme", "gsearch")
        self.assertEqual(resp["serpwow_summary"]["available_files"], ["run.log"])

    def test_status_checks_s3_for_missing_reporting_files(self):
        class FakeS3:
            def __init__(self):
                self.calls = []

            def list_objects_v2(self, **kwargs):
                self.calls.append(kwargs)
                return {"Contents": [
                    {"Key": "acme/gsearch/gm1/run.log"},
                    {"Key": "acme/gsearch/gm1/unrelated.txt"},
                ],
                    "CommonPrefixes": [
                        {"Prefix": "acme/gsearch/gm1/errors/"},
                        {"Prefix": "acme/gsearch/gm1/serpwow_response/"},
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
             mock.patch.dict("os.environ", {"S3_BUCKET": "bucket",
                                            "SERPWOW_S3_STATE_FLUSH_SEC": "0"}):
            resp = asyncio.run(legacy_app.upload_status("gm1"))
        self.assertEqual(resp["serpwow_summary"]["available_files"], ["run.log"])
        self.assertEqual(len(s3.calls), 1)
        self.assertEqual(s3.calls[0]["Bucket"], "bucket")
        self.assertEqual(s3.calls[0]["Prefix"], "acme/gsearch/gm1/")
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
                 mock.patch.dict("os.environ", {"S3_BUCKET": "bucket",
                                            "SERPWOW_S3_STATE_FLUSH_SEC": "0"}):
                resp = asyncio.run(legacy_app.upload_status("gm1"))
        self.assertEqual(resp["serpwow_summary"]["available_files"],
                         ["found.csv", "notFound.csv", "report.json", "run.log"])
        s3.list_objects_v2.assert_not_called()
        resolve.assert_not_called()

    def test_normal_s3_write_seeds_exact_run_prefix_before_background_write(self):
        scheduled = []

        def capture_task(coro):
            scheduled.append(coro)
            return mock.Mock()

        state = {"company_name": "Acme Inc", "pipeline": "gsearch"}
        write_s3 = mock.Mock()
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(legacy_app, "_state_file",
                               return_value=Path(td) / "state.json"), \
             mock.patch.object(legacy_app, "_find_upload_dir", return_value=Path(td)), \
             mock.patch.object(legacy_app, "_write_json"), \
             mock.patch.object(legacy_app, "_write_json_to_s3_sync", write_s3), \
             mock.patch.object(legacy_app.asyncio, "create_task",
                               side_effect=capture_task), \
             mock.patch.dict("os.environ", {"S3_BUCKET": "bucket",
                                            "SERPWOW_S3_STATE_FLUSH_SEC": "0"}):
            asyncio.run(legacy_app.write_upload_artifact("up-1", "state", state))
            asyncio.run(scheduled.pop())

        self.assertEqual(legacy_app._s3_run_prefix_cache.get("up-1"),
                         "acme-inc/gsearch/up-1")
        write_s3.assert_called_once_with(
            "acme-inc/gsearch/up-1/state.json", state)

    def test_normal_s3_write_retains_resolved_legacy_run_prefix(self):
        scheduled = []

        def capture_task(coro):
            scheduled.append(coro)
            return mock.Mock()

        legacy_app._s3_run_prefix_cache["gm1"] = "ISI_Market_Test/gsearch/gm1"
        state = {"company_name": "ISI Market Test", "pipeline": "gsearch"}
        write_s3 = mock.Mock()
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(legacy_app, "_output_file",
                               return_value=Path(td) / "output.json"), \
             mock.patch.object(legacy_app, "_find_upload_dir", return_value=Path(td)), \
             mock.patch.object(legacy_app, "_write_json"), \
             mock.patch.object(legacy_app, "_write_json_to_s3_sync", write_s3), \
             mock.patch.object(legacy_app.asyncio, "create_task",
                               side_effect=capture_task), \
             mock.patch.dict("os.environ", {"S3_BUCKET": "bucket",
                                            "SERPWOW_S3_STATE_FLUSH_SEC": "0"}):
            asyncio.run(legacy_app.write_upload_artifact("gm1", "output", state))
            asyncio.run(scheduled.pop())

        self.assertEqual(legacy_app._s3_run_prefix_cache["gm1"],
                         "ISI_Market_Test/gsearch/gm1")
        write_s3.assert_called_once_with(
            "ISI_Market_Test/gsearch/gm1/output.json", state)

    def test_s3_file_links_use_resolved_legacy_run_prefix(self):
        legacy_app._s3_run_prefix_cache["gm1"] = "ISI_Market_Test/gsearch/gm1"
        with mock.patch.dict("os.environ", {"S3_BUCKET": "bucket",
                                            "SERPWOW_S3_STATE_FLUSH_SEC": "0"}):
            links = legacy_app._upload_file_links(
                "gm1", "ISI Market Test", "gsearch")

        self.assertEqual(links["state.json"],
                         "s3://bucket/ISI_Market_Test/gsearch/gm1/state.json")
        self.assertEqual(links["run.log"],
                         "s3://bucket/ISI_Market_Test/gsearch/gm1/run.log")

    def test_restart_restores_legacy_sidecar_without_suffix_scan(self):
        legacy_app._s3_run_prefix_cache.clear()
        scheduled = []

        def capture_task(coro):
            scheduled.append(coro)
            return mock.Mock()

        state = {"upload_id": "gm1", "company_name": "ISI Market Test",
                 "pipeline": "gsearch"}
        resolve = mock.Mock()
        write_s3 = mock.Mock()
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            state_path = run_dir / "state.json"
            state_path.write_text(json.dumps(state))
            (run_dir / ".s3_prefix").write_text("ISI_Market_Test/gsearch/gm1")
            with mock.patch.object(legacy_app, "_state_file", return_value=state_path), \
                 mock.patch.object(legacy_app, "_find_upload_dir", return_value=run_dir), \
                 mock.patch.object(legacy_app, "_find_s3_upload_key_sync", resolve), \
                 mock.patch.object(legacy_app, "_write_json_to_s3_sync", write_s3), \
                 mock.patch.object(legacy_app.asyncio, "create_task",
                                   side_effect=capture_task), \
                 mock.patch.dict("os.environ", {"S3_BUCKET": "bucket",
                                            "SERPWOW_S3_STATE_FLUSH_SEC": "0"}):
                restored = asyncio.run(
                    legacy_app.read_upload_artifact("gm1", "state"))
                asyncio.run(
                    legacy_app.write_upload_artifact("gm1", "state", restored))
                asyncio.run(scheduled.pop())

        self.assertEqual(restored, state)
        self.assertNotIn("s3_prefix", restored)
        resolve.assert_not_called()
        write_s3.assert_called_once_with(
            "ISI_Market_Test/gsearch/gm1/state.json", state)

    def test_restart_resolves_legacy_prefix_once_and_persists_sidecar(self):
        legacy_app._s3_run_prefix_cache.clear()
        scheduled = []

        def capture_task(coro):
            scheduled.append(coro)
            return mock.Mock()

        state = {"upload_id": "gm1", "company_name": "ISI Market Test",
                 "pipeline": "gsearch"}
        legacy_key = "ISI_Market_Test/gsearch/gm1/state.json"
        resolve = mock.Mock(return_value=legacy_key)
        write_s3 = mock.Mock()
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            state_path = run_dir / "state.json"
            sidecar = run_dir / ".s3_prefix"
            state_path.write_text(json.dumps(state))
            with mock.patch.object(legacy_app, "_state_file", return_value=state_path), \
                 mock.patch.object(legacy_app, "_find_upload_dir", return_value=run_dir), \
                 mock.patch.object(legacy_app, "_find_s3_upload_key_sync", resolve), \
                 mock.patch.object(legacy_app, "_write_json_to_s3_sync", write_s3), \
                 mock.patch.object(legacy_app.asyncio, "create_task",
                                   side_effect=capture_task), \
                 mock.patch.dict("os.environ", {"S3_BUCKET": "bucket",
                                            "SERPWOW_S3_STATE_FLUSH_SEC": "0"}):
                restored = asyncio.run(
                    legacy_app.read_upload_artifact("gm1", "state"))
                self.assertTrue(sidecar.exists())
                self.assertEqual(sidecar.read_text(), "ISI_Market_Test/gsearch/gm1")

                legacy_app._s3_run_prefix_cache.clear()
                asyncio.run(
                    legacy_app.write_upload_artifact("gm1", "state", restored))
                asyncio.run(scheduled.pop())

                legacy_app._s3_run_prefix_cache.clear()
                reread = asyncio.run(
                    legacy_app.read_upload_artifact("gm1", "state"))

        self.assertEqual(reread, state)
        self.assertEqual(resolve.call_count, 1)
        write_s3.assert_called_once_with(legacy_key, state)

    def test_new_s3_write_persists_normalized_prefix_sidecar(self):
        legacy_app._s3_run_prefix_cache.clear()
        scheduled = []

        def capture_task(coro):
            scheduled.append(coro)
            return mock.Mock()

        state = {"upload_id": "new1", "company_name": "Acme Inc",
                 "pipeline": "gsearch"}
        write_s3 = mock.Mock()
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            state_path = run_dir / "state.json"
            with mock.patch.object(legacy_app, "_state_file", return_value=state_path), \
                 mock.patch.object(legacy_app, "_find_upload_dir", return_value=run_dir), \
                 mock.patch.object(legacy_app, "_write_json_to_s3_sync", write_s3), \
                 mock.patch.object(legacy_app.asyncio, "create_task",
                                   side_effect=capture_task), \
                 mock.patch.dict("os.environ", {"S3_BUCKET": "bucket",
                                            "SERPWOW_S3_STATE_FLUSH_SEC": "0"}):
                asyncio.run(
                    legacy_app.write_upload_artifact("new1", "state", state))
                asyncio.run(scheduled.pop())
            self.assertTrue((run_dir / ".s3_prefix").exists())
            sidecar_value = (run_dir / ".s3_prefix").read_text()
            persisted_state = json.loads(state_path.read_text())

        self.assertEqual(sidecar_value, "acme-inc/gsearch/new1")
        self.assertEqual(persisted_state, state)
        write_s3.assert_called_once_with(
            "acme-inc/gsearch/new1/state.json", state)

    def test_cached_current_prefix_lists_once_without_suffix_scan(self):
        class FakeS3:
            def __init__(self):
                self.calls = []

            def list_objects_v2(self, **kwargs):
                self.calls.append(kwargs)
                return {"Contents": [{"Key": "acme/gsearch/gm1/run.log"}]}

        s3 = FakeS3()
        resolve = mock.Mock()
        legacy_app._s3_run_prefix_cache["gm1"] = "acme/gsearch/gm1"
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(legacy_app, "_find_upload_dir", return_value=Path(td)), \
             mock.patch.object(legacy_app, "get_s3_client", return_value=s3), \
             mock.patch.object(legacy_app, "_find_s3_upload_key_sync", resolve), \
             mock.patch.dict("os.environ", {"S3_BUCKET": "bucket",
                                            "SERPWOW_S3_STATE_FLUSH_SEC": "0"}):
            available = asyncio.run(legacy_app._available_reporting_files(
                "gm1", "Acme", "gsearch"))

        self.assertEqual(available, ["run.log"])
        self.assertEqual(len(s3.calls), 1)
        self.assertEqual(s3.calls[0]["Prefix"], "acme/gsearch/gm1/")
        resolve.assert_not_called()

    def test_reading_legacy_s3_state_caches_actual_run_prefix(self):
        legacy_key = "ISI_Market_Test/gsearch/gm1/state.json"
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(legacy_app, "_state_file", return_value=Path(td) / "state.json"), \
             mock.patch.object(legacy_app, "_find_upload_dir", return_value=Path(td)), \
             mock.patch.object(legacy_app, "_find_s3_upload_key_sync", return_value=legacy_key), \
             mock.patch.object(legacy_app, "_read_json_from_s3_sync", return_value={"upload_id": "gm1"}), \
             mock.patch.dict("os.environ", {"S3_BUCKET": "bucket",
                                            "SERPWOW_S3_STATE_FLUSH_SEC": "0"}):
            asyncio.run(legacy_app.read_upload_artifact("gm1", "state"))
        self.assertEqual(legacy_app._s3_run_prefix_cache["gm1"],
                         "ISI_Market_Test/gsearch/gm1")

    def test_legacy_s3_prefix_resolution_is_cached_after_first_availability_check(self):
        class LegacyS3:
            def __init__(self):
                self.calls = []

            def list_objects_v2(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs["Prefix"] == "ISI_Market_Test/gsearch/gm1/":
                    return {"Contents": [
                        {"Key": "ISI_Market_Test/gsearch/gm1/found.csv"},
                        {"Key": "ISI_Market_Test/gsearch/gm1/run.log"},
                    ]}
                return {"Contents": []}

        s3 = LegacyS3()
        resolve = mock.Mock(return_value="ISI_Market_Test/gsearch/gm1/state.json")
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(legacy_app, "_find_upload_dir", return_value=Path(td)), \
             mock.patch.object(legacy_app, "get_s3_client", return_value=s3), \
             mock.patch.object(legacy_app, "_find_s3_upload_key_sync", resolve), \
             mock.patch.dict("os.environ", {"S3_BUCKET": "bucket",
                                            "SERPWOW_S3_STATE_FLUSH_SEC": "0"}):
            first = asyncio.run(legacy_app._available_reporting_files(
                "gm1", "ISI Market Test", "gsearch"))
            first_calls = list(s3.calls)
            second = asyncio.run(legacy_app._available_reporting_files(
                "gm1", "ISI Market Test", "gsearch"))

        self.assertEqual(first, ["found.csv", "run.log"])
        self.assertEqual(second, first)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual([call["Prefix"] for call in first_calls], [
            "isi-market-test/gsearch/gm1/",
            "ISI_Market_Test/gsearch/gm1/",
        ])
        self.assertEqual(len(s3.calls), 3)
        self.assertEqual(s3.calls[-1]["Prefix"], "ISI_Market_Test/gsearch/gm1/")
        self.assertTrue(all(call["Delimiter"] == "/" for call in s3.calls))
