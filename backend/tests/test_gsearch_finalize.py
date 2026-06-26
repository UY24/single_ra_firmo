import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.serpwow import legacy_app


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
    def test_writes_files_and_mirrors(self):
        with tempfile.TemporaryDirectory() as d:
            upload_dir = Path(d) / "isi" / "up1"
            upload_dir.mkdir(parents=True)
            with mock.patch.object(legacy_app, "_find_upload_dir", return_value=upload_dir), \
                 mock.patch.dict("os.environ", {"S3_BUCKET": "bkt"}), \
                 mock.patch("app.core.s3.upload_file") as up:
                asyncio.run(legacy_app._finalize_gsearch_outputs("up1", _state()))
            self.assertTrue((upload_dir / "found.csv").exists())
            self.assertTrue((upload_dir / "report.json").exists())
            # 4 files mirrored
            self.assertEqual(up.call_count, 4)
            keys = sorted(c.args[1] for c in up.call_args_list)
            self.assertTrue(all(k.startswith("acme-motors/gsearch/up1/") for k in keys))
