import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.serpwow import engine as legacy_app


def _state():
    return {"upload_id": "up1", "company_name": "Acme Motors", "pipeline": "gsearch",
            "status": "completed",
            "rows": [{"row_index": 0, "company_name": "Acme Motors", "country": "us",
                      "status": "completed", "error": None,
                      "result": {"official_website": "https://acme.com", "gemini_cost_usd": 0.0,
                                 "context": {"cost_breakdown": {"serpwow_request_count": 2},
                                             "formatted_results": [],
                                             "final_url_selection_ai": {"used": True, "usage": {},
                                                 "raw": {"official_website": "https://acme.com",
                                                         "confidence_score": 80, "confidence": "high"}}}}}]}


class TestFinalizeGsearch(unittest.TestCase):
    def tearDown(self):
        legacy_app._s3_run_prefix_cache.clear()

    def test_finalize_reads_batch_confidence(self):
        state = _state()
        # simulate batch-only confidence (no per-row final_url_selection_ai)
        ctx = state["rows"][0]["result"]["context"]
        ctx.pop("final_url_selection_ai", None)
        ctx["gemini_batch_ai"] = {"used": True, "usage": {},
            "raw": {"official_website": "https://acme.com", "confidence_score": 73, "confidence": "medium"}}
        import csv
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            upload_dir = Path(d) / "isi" / "up1"; upload_dir.mkdir(parents=True)
            with mock.patch.object(legacy_app, "_find_upload_dir", return_value=upload_dir), \
                 mock.patch.dict("os.environ", {}, clear=False):
                asyncio.run(legacy_app._finalize_serpwow_outputs("up1", state))
            with (upload_dir / "found.csv").open() as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(rows[0]["confidence"], "73")

    def test_writes_files_and_mirrors(self):
        with tempfile.TemporaryDirectory() as d:
            upload_dir = Path(d) / "isi" / "up1"
            upload_dir.mkdir(parents=True)
            with mock.patch.object(legacy_app, "_find_upload_dir", return_value=upload_dir), \
                 mock.patch.dict("os.environ", {"S3_BUCKET": "bkt"}), \
                 mock.patch("app.core.s3.upload_file") as up:
                asyncio.run(legacy_app._finalize_serpwow_outputs("up1", _state()))
            self.assertTrue((upload_dir / "found.csv").exists())
            self.assertTrue((upload_dir / "report.json").exists())
            # 4 files mirrored
            self.assertEqual(up.call_count, 4)
            keys = sorted(c.args[1] for c in up.call_args_list)
            self.assertTrue(all(k.startswith("acme-motors/gsearch/up1/") for k in keys))

    def test_mirrors_every_reporting_file_to_resolved_legacy_prefix(self):
        legacy_app._s3_run_prefix_cache["up1"] = "Acme_Motors/gsearch/up1"
        paths = {
            name: Path(f"/tmp/{name}")
            for name in ("found.csv", "notFound.csv", "report.json", "run.log")
        }
        with mock.patch.object(legacy_app.reporting, "write_outputs",
                               return_value=paths), \
             mock.patch.object(legacy_app, "_write_error_dumps", return_value={}), \
             mock.patch.object(legacy_app, "_find_upload_dir", return_value=Path("/tmp")), \
             mock.patch.dict("os.environ", {"S3_BUCKET": "bkt"}), \
             mock.patch("app.core.s3.upload_file") as upload_file:
            asyncio.run(legacy_app._finalize_serpwow_outputs("up1", _state()))

        self.assertEqual(
            {call.args[1] for call in upload_file.call_args_list},
            {f"Acme_Motors/gsearch/up1/{name}" for name in paths},
        )
