import unittest
from app.services.ai_mode.ai_mode_service import _build_run_update


class TestBuildRunUpdate(unittest.TestCase):
    def _summary(self, **overrides):
        base = {
            "status": "completed",
            "websites_found": 10,
            "websites_not_found": 2,
            "llm_errors": 1,
            "failed_request_count": 0,
            "token_usage": {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300},
            "cost": {"llm_usd": 0.002, "scrapedo_searches": 5, "total_usd": 0.002},
            "batch_duration_seconds": 42.5,
            "completed_at": "2026-06-15T10:00:00Z",
            "model": "gemini-2.5-flash-lite",
            "is_batch": False,
        }
        base.update(overrides)
        return base

    def test_model_included_in_update(self):
        result = _build_run_update(self._summary(), {})
        self.assertEqual(result["model"], "gemini-2.5-flash-lite")

    def test_is_batch_included_in_update(self):
        result = _build_run_update(self._summary(), {})
        self.assertIs(result["is_batch"], False)

    def test_is_batch_true(self):
        result = _build_run_update(self._summary(is_batch=True), {})
        self.assertIs(result["is_batch"], True)

    def test_model_missing_returns_none(self):
        summary = self._summary()
        del summary["model"]
        result = _build_run_update(summary, {})
        self.assertIsNone(result["model"])

    def test_is_batch_missing_returns_none(self):
        summary = self._summary()
        del summary["is_batch"]
        result = _build_run_update(summary, {})
        self.assertIsNone(result["is_batch"])


if __name__ == "__main__":
    unittest.main()
