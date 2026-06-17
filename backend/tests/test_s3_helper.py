"""Offline unit tests for app.core.s3 (boto3 client mocked; no network)."""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from app.core import s3


class BucketNameTests(unittest.TestCase):
    def test_raises_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                s3.bucket_name()

    def test_is_configured(self) -> None:
        with patch.dict(os.environ, {"S3_BUCKET": "website-url-finder"}, clear=True):
            self.assertTrue(s3.is_configured())
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(s3.is_configured())


class UploadDirectoryTests(unittest.TestCase):
    def test_uploads_every_file_with_relative_keys(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "runX"
            (root / "raw_responses").mkdir(parents=True)
            (root / "final_report.json").write_text("{}", encoding="utf-8")
            (root / "raw_responses" / "request_001.json").write_text("{}", encoding="utf-8")

            fake_client = MagicMock()
            with patch.dict(os.environ, {"S3_BUCKET": "website-url-finder"}, clear=True), \
                 patch("app.core.s3.get_s3_client", return_value=fake_client):
                keys = s3.upload_directory(root, "acme-inc/ai_bulk/runX")

            self.assertEqual(
                keys,
                ["acme-inc/ai_bulk/runX/final_report.json",
                 "acme-inc/ai_bulk/runX/raw_responses/request_001.json"],
            )
            self.assertEqual(fake_client.upload_file.call_count, 2)


if __name__ == "__main__":
    unittest.main()
