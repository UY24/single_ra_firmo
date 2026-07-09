"""Offline tests for the 3-line found/not_found/errored Slack outcome + error-source line."""
import unittest
from unittest.mock import patch

from app.core import notify


class TestSlackOutcome(unittest.TestCase):
    def test_three_line_outcome_and_errors(self):
        captured = {}

        def fake_post(fallback, blocks):
            captured["fallback"] = fallback
            captured["blocks"] = blocks
            return True

        with patch.object(notify, "_post", side_effect=fake_post), \
             patch.object(notify, "is_configured", return_value=True):
            notify.notify_run_complete(pipeline="gsearch", company="C", run_ref="r",
                                       status="completed_with_errors", found=2, not_found=1,
                                       errored=2, error_sources={"serpwow": 2})

        blob = str(captured["blocks"])
        self.assertIn("found", blob.lower())
        self.assertIn("not found", blob.lower())
        self.assertIn("errored", blob.lower())
        self.assertIn("serpwow", blob.lower())
        # fallback: "2 found / 1 not found / 2 errored"
        self.assertIn("2 errored", captured["fallback"])
        self.assertIn("2 found", captured["fallback"])
        self.assertIn("1 not found", captured["fallback"])

    def test_error_sources_sorted_by_count_desc(self):
        captured = {}

        def fake_post(fallback, blocks):
            captured["fallback"] = fallback
            captured["blocks"] = blocks
            return True

        with patch.object(notify, "_post", side_effect=fake_post), \
             patch.object(notify, "is_configured", return_value=True):
            notify.notify_run_complete(pipeline="gsearch", company="C", run_ref="r",
                                       status="completed_with_errors", found=1, not_found=0,
                                       errored=3, error_sources={"gemini": 1, "serpwow": 2})

        # Find the Errors field text and confirm serpwow (higher count) is listed first.
        errors_field = next(f for f in captured["blocks"][3]["fields"] if "Errors" in f["text"])
        serpwow_idx = errors_field["text"].find("serpwow")
        gemini_idx = errors_field["text"].find("gemini")
        self.assertNotEqual(serpwow_idx, -1)
        self.assertNotEqual(gemini_idx, -1)
        self.assertLess(serpwow_idx, gemini_idx)

    def test_no_errored_keeps_two_line_outcome(self):
        captured = {}

        def fake_post(fallback, blocks):
            captured["fallback"] = fallback
            captured["blocks"] = blocks
            return True

        with patch.object(notify, "_post", side_effect=fake_post), \
             patch.object(notify, "is_configured", return_value=True):
            notify.notify_run_complete(pipeline="gsearch", company="C", run_ref="r",
                                       status="completed", found=2, not_found=1)

        blob = str(captured["blocks"])
        self.assertNotIn("errored", blob.lower())
        self.assertNotIn("Errors", str(captured["blocks"]))
        self.assertEqual(captured["fallback"], "Completed: gsearch · C · 2 found / 1 not found")


if __name__ == "__main__":
    unittest.main()
