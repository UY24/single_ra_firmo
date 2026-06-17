"""Offline unit tests for AI Mode S3 mirror (core.s3 mocked)."""
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.ai_mode import s3_sync


class KeyPrefixTests(unittest.TestCase):
    def test_prefix_from_run_dir(self) -> None:
        run_dir = Path("/x/ai_mode_results/acme-inc/run-123")
        self.assertEqual(s3_sync.s3_key_prefix(run_dir, "ai_bulk"),
                         "acme-inc/ai_bulk/run-123")


class MirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_dir = Path("/x/ai_mode_results/acme-inc/run-123")

    def test_skips_when_not_configured(self) -> None:
        with patch("app.services.ai_mode.s3_sync.s3.is_configured", return_value=False):
            self.assertEqual(s3_sync.mirror_run_to_s3(self.run_dir, "ai_deep"), [])

    def test_uploads_when_configured(self) -> None:
        with patch("app.services.ai_mode.s3_sync.s3.is_configured", return_value=True), \
             patch("app.services.ai_mode.s3_sync.s3.bucket_name", return_value="website-url-finder"), \
             patch("app.services.ai_mode.s3_sync.s3.upload_directory",
                   return_value=["acme-inc/ai_deep/run-123/final_report.json"]) as up:
            keys = s3_sync.mirror_run_to_s3(self.run_dir, "ai_deep")
        up.assert_called_once_with(self.run_dir, "acme-inc/ai_deep/run-123")
        self.assertEqual(keys, ["acme-inc/ai_deep/run-123/final_report.json"])

    def test_swallows_errors(self) -> None:
        with patch("app.services.ai_mode.s3_sync.s3.is_configured", return_value=True), \
             patch("app.services.ai_mode.s3_sync.s3.upload_directory",
                   side_effect=RuntimeError("boom")):
            self.assertEqual(s3_sync.mirror_run_to_s3(self.run_dir, "ai_bulk"), [])


if __name__ == "__main__":
    unittest.main()
