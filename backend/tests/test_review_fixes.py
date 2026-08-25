# backend/tests/test_review_fixes.py
"""Pins three code-review fixes:

1. prepare_ai_mode_run validates the LLM config BEFORE creating the run dir, so
   a missing API key raises ValueError without leaving an orphan run dir, and
   the upload endpoint maps it to HTTP 400 (not 500).
2. sanitize_secret_text fully redacts tokens containing the letter 's'
   (the old character class had a literal backslash + 's' instead of \\s).
3. (batch_size env tolerance is pinned in test_mode_config.py)
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.models.entities import InvalidCSVError
from app.services.ai_mode import ai_mode_service, run_store

CSV_OK = b"company_name,country\nAcme,Japan\nBeta,Japan\n"

# Force the gemini provider with NO api key configured (values of "" are
# stripped to empty by _str_env, so LLMConfig.validate() raises ValueError).
NO_KEY_ENV = {
    "GEMINI_API_KEY": "",
    "LLM_BATCH": "",
}


class TestSanitizeSecretText(unittest.TestCase):
    def test_token_containing_s_is_fully_redacted(self):
        out = ai_mode_service.sanitize_secret_text(
            "https://api.scrape.do/?token=abc123secret456&url=x"
        )
        self.assertNotIn("abc123secret456", out)
        self.assertNotIn("secret456", out)
        self.assertIn("?token=[REDACTED]&url=x", out)

    def test_token_as_second_query_param(self):
        out = ai_mode_service.sanitize_secret_text(
            "GET https://api.scrape.do/?url=x&token=ssssss failed"
        )
        self.assertNotIn("ssssss", out)
        self.assertIn("&token=[REDACTED] failed", out)

    def test_redaction_stops_at_whitespace_and_quotes(self):
        out = ai_mode_service.sanitize_secret_text("'?token=sss' next=keep")
        self.assertEqual(out, "'?token=[REDACTED]' next=keep")


class _PreparedEnv(unittest.TestCase):
    """Shared tempdir + env scaffolding for prepare/router tests."""

    extra_env: dict[str, str] = NO_KEY_ENV

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.results_root = Path(self._tmp.name) / "ai_mode_results"
        self._patches = [
            mock.patch.dict(os.environ, self.extra_env),
            mock.patch.object(run_store, "AI_MODE_RESULTS_DIR", self.results_root),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _run_dirs(self) -> list[Path]:
        if not self.results_root.exists():
            return []
        return [p for p in self.results_root.glob("*/*") if p.is_dir()]


class TestPrepareValidatesConfigBeforeWritingFiles(_PreparedEnv):
    def _prepare(self):
        return ai_mode_service.prepare_ai_mode_run(
            CSV_OK, "input.csv",
            mode_key="ai_bulk", company_name="Acme Corp", company_id="acme-id-1",
        )

    def test_missing_llm_key_raises_value_error_and_leaves_no_run_dir(self):
        with self.assertRaises(ValueError) as ctx:
            self._prepare()
        # It is a config error, not a CSV error.
        self.assertNotIsInstance(ctx.exception, InvalidCSVError)
        self.assertEqual(self._run_dirs(), [])

    def test_failure_after_input_csv_written_cleans_up_run_dir(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}), \
                mock.patch.object(ai_mode_service, "_persist_status",
                                  side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._prepare()
        self.assertEqual(self._run_dirs(), [])

    def test_prepare_succeeds_with_key_and_writes_run_dir(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}):
            info = self._prepare()
        run_dirs = self._run_dirs()
        self.assertEqual(len(run_dirs), 1)
        self.assertEqual(run_dirs[0].name, info["run_id"])
        self.assertTrue((run_dirs[0] / "input.csv").exists())
        self.assertTrue((run_dirs[0] / "status.json").exists())


class TestUploadEndpointMapsConfigErrorTo400(_PreparedEnv):
    def _post(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers.ai_mode import router

        app = FastAPI()
        app.include_router(router)
        # Offline: the upload endpoint now validates company_id against Supabase
        # (Task 14); stub the service so no network is touched.
        svc = mock.MagicMock()
        svc.get_company.return_value = {"id": "acme-id-1", "name": "Acme Corp"}
        svc.create_run.return_value = None
        with mock.patch("app.routers.ai_mode.get_company_service", return_value=svc), \
                mock.patch("app.services.ai_mode.broker.is_ready", return_value=True), \
                TestClient(app) as client:
            return client.post(
                "/uploads/ai-mode",
                files={"file": ("input.csv", CSV_OK, "text/csv")},
                data={"mode": "ai_bulk", "company_id": "acme-id-1"},
            )

    def test_missing_llm_key_returns_400_with_message(self):
        response = self._post()
        self.assertEqual(response.status_code, 400)
        self.assertIn("Required", response.json()["detail"])
        self.assertEqual(self._run_dirs(), [])


if __name__ == "__main__":
    unittest.main()
