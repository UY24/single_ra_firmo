import asyncio
import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.serpwow import engine as legacy_app


def _state():
    return {"upload_id": "gm1", "company_name": "Acme Motors", "pipeline": "gmaps",
            "status": "completed",
            "rows": [{"row_index": 0, "company_name": "Acme Motors", "country": "us",
                      "status": "completed", "error": None,
                      "result": {"official_website": "https://acme-motors.com",
                                 "gemini_cost_usd": 0.0,
                                 "context": {"cost_breakdown": {"serpwow_request_count": 1},
                                             "gmaps_confidence": {"mode": "heuristic", "raw": {
                                                 "official_website": "https://acme-motors.com",
                                                 "confidence_score": 90, "confidence": "high",
                                                 "reason": "name+address"}}}}}]}


class TestGmapsFinalize(unittest.TestCase):
    def test_writes_files_and_confidence(self):
        with tempfile.TemporaryDirectory() as d:
            upload_dir = Path(d) / "isi" / "gm1"
            upload_dir.mkdir(parents=True)
            with mock.patch.object(legacy_app, "_find_upload_dir", return_value=upload_dir), \
                 mock.patch.dict("os.environ", {}, clear=False):
                asyncio.run(legacy_app._finalize_serpwow_outputs("gm1", _state()))
            self.assertTrue((upload_dir / "found.csv").exists())
            self.assertTrue((upload_dir / "notFound.csv").exists())
            self.assertTrue((upload_dir / "report.json").exists())
            self.assertTrue((upload_dir / "run.log").exists())
            with (upload_dir / "found.csv").open(encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(rows[0]["website_url"], "https://acme-motors.com")
            self.assertEqual(rows[0]["confidence"], "90")

    def test_mirrors_to_s3_under_gmaps_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            upload_dir = Path(d) / "isi" / "gm1"
            upload_dir.mkdir(parents=True)
            with mock.patch.object(legacy_app, "_find_upload_dir", return_value=upload_dir), \
                 mock.patch.dict("os.environ", {"S3_BUCKET": "bkt"}), \
                 mock.patch("app.core.s3.upload_file") as up:
                asyncio.run(legacy_app._finalize_serpwow_outputs("gm1", _state()))
            keys = sorted(c.args[1] for c in up.call_args_list)
            self.assertTrue(all(k.startswith("acme-motors/gmaps/gm1/") for k in keys))


def _mixed_state():
    """A completed_with_errors gmaps upload with one found row (has a
    gmaps_confidence raw block) and one not-found row (no website, an
    error, no confidence)."""
    return {"upload_id": "gm2", "company_name": "Acme Motors", "pipeline": "gmaps",
            "status": "completed_with_errors",
            "rows": [
                {"row_index": 0, "company_name": "Acme Motors", "country": "us",
                 "status": "completed", "error": None,
                 "result": {"official_website": "https://acme-motors.com",
                            "gemini_cost_usd": 0.0,
                            "context": {"cost_breakdown": {"serpwow_request_count": 1},
                                        "gmaps_confidence": {"mode": "heuristic", "raw": {
                                            "official_website": "https://acme-motors.com",
                                            "confidence_score": 90, "confidence": "high",
                                            "reason": "name+address"}}}}},
                {"row_index": 1, "company_name": "Ghost Corp", "country": "us",
                 "status": "failed", "error": "No Google Maps listing found.",
                 "result": {"official_website": None,
                            "gemini_cost_usd": 0.0,
                            "context": {"cost_breakdown": {"serpwow_request_count": 1}}}},
            ]}


class TestGmapsFinalizeCompletedWithErrors(unittest.TestCase):
    def test_found_and_not_found_rows_split_correctly(self):
        with tempfile.TemporaryDirectory() as d:
            upload_dir = Path(d) / "isi" / "gm2"
            upload_dir.mkdir(parents=True)
            with mock.patch.object(legacy_app, "_find_upload_dir", return_value=upload_dir), \
                 mock.patch.dict("os.environ", {}, clear=False):
                asyncio.run(legacy_app._finalize_serpwow_outputs("gm2", _mixed_state()))

            with (upload_dir / "found.csv").open(encoding="utf-8-sig") as fh:
                found_rows = list(csv.DictReader(fh))
            with (upload_dir / "notFound.csv").open(encoding="utf-8-sig") as fh:
                not_found_rows = list(csv.DictReader(fh))

            self.assertEqual(len(found_rows), 1)
            self.assertEqual(found_rows[0]["company_name"], "Acme Motors")
            self.assertEqual(found_rows[0]["website_url"], "https://acme-motors.com")
            self.assertEqual(found_rows[0]["confidence"], "90")

            self.assertEqual(len(not_found_rows), 1)
            self.assertEqual(not_found_rows[0]["company_name"], "Ghost Corp")
            self.assertEqual(not_found_rows[0]["website_url"], "")
            self.assertEqual(not_found_rows[0]["error"], "No Google Maps listing found.")
