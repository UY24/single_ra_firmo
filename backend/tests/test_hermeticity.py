import os
import unittest


class TestHermeticity(unittest.TestCase):
    def test_cloud_env_blanked(self):
        for key in (
            "SLACK_WEBHOOK_URL", "S3_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
            "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "GEMINI_API_KEY", "SERPWOW_API_KEY",
        ):
            self.assertEqual(os.environ.get(key, ""), "", f"{key} must be blank in tests")

    def test_helpers_report_unconfigured(self):
        from app.core import s3 as core_s3
        from app.core import notify
        self.assertFalse(core_s3.is_configured())
        self.assertFalse(notify.is_configured())


if __name__ == "__main__":
    unittest.main()
