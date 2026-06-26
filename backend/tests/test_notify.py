"""Offline unit tests for app.core.notify (httpx mocked; no network)."""
import os
import unittest
from unittest import mock

from app.core import notify

WEBHOOK = "https://hooks.slack.com/services/T000/B000/xxxx"


class IsConfiguredTests(unittest.TestCase):
    def test_unset_or_blank_is_not_configured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(notify.is_configured())
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": "   "}, clear=True):
            self.assertFalse(notify.is_configured())

    def test_set_is_configured(self):
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": WEBHOOK}, clear=True):
            self.assertTrue(notify.is_configured())


class PostTests(unittest.TestCase):
    def test_noop_when_unconfigured(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("httpx.Client") as client:
            self.assertFalse(notify._post("hi"))
        client.assert_not_called()  # never touch the network when disabled

    def test_posts_text_payload_to_webhook(self):
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": WEBHOOK}, clear=True), \
                mock.patch("httpx.Client") as client_cls:
            inst = client_cls.return_value.__enter__.return_value  # used as context manager
            inst.post.return_value = mock.MagicMock(status_code=200)
            self.assertTrue(notify._post("hello world"))
        inst.post.assert_called_once_with(WEBHOOK, json={"text": "hello world"})

    def test_posts_blocks_payload_to_webhook(self):
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": "Run completed"}}]
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": WEBHOOK}, clear=True), \
                mock.patch("httpx.Client") as client_cls:
            inst = client_cls.return_value.__enter__.return_value
            inst.post.return_value = mock.MagicMock(status_code=200)
            self.assertTrue(notify._post("hello world", blocks))
        inst.post.assert_called_once_with(WEBHOOK, json={"text": "hello world", "blocks": blocks})

    def test_http_error_status_returns_false(self):
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": WEBHOOK}, clear=True), \
                mock.patch("httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.post.return_value = mock.MagicMock(
                status_code=404, text="no_service")
            self.assertFalse(notify._post("hello"))

    def test_exception_is_swallowed(self):
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": WEBHOOK}, clear=True), \
                mock.patch("httpx.Client", side_effect=RuntimeError("boom")):
            self.assertFalse(notify._post("hello"))  # must not raise


class FormatterTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": WEBHOOK}, clear=True)
        self.env.start()
        self.post = mock.patch("app.core.notify._post", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.env.stop)

    def _fallback(self):
        return self.post.call_args.args[0]

    def _blocks_text(self):
        """Flatten all block text (header/section/fields/context) into one string."""
        import json
        call = self.post.call_args
        blocks = call.args[1] if len(call.args) > 1 else call.kwargs.get("blocks")
        return json.dumps(blocks, ensure_ascii=False)

    def test_complete_clean_found_counts_and_full_detail(self):
        notify.notify_run_complete(pipeline="AI Mode 1 — Bulk", company="Acme Corp",
                                   run_ref="r1", status="completed",
                                   found=1840, not_found=160, total_rows=2000,
                                   searches=3300, search_label="Scrape.do searches",
                                   tokens=1_234_567,
                                   input_tokens=1_000_000, output_tokens=234_567,
                                   cost_usd=4.5, duration_seconds=7980, llm_errors=0)
        blocks = self._blocks_text()
        self.assertIn("Run completed", blocks)                   # header (with ✅)
        self.assertIn("*AI Mode 1 — Bulk*", blocks)              # run type name shown
        self.assertIn("Company: Acme Corp", blocks)
        self.assertIn("Status: `completed`", blocks)
        # scannable field grid:
        self.assertIn("Outcome", blocks)
        self.assertIn("*1,840* found", blocks)
        self.assertIn("*160* not found", blocks)
        self.assertIn("Rows", blocks)
        self.assertIn("Scrape.do searches", blocks)              # context-aware label
        self.assertIn("1.0M input / 235k output", blocks)
        self.assertIn("Cost", blocks)
        self.assertIn("$4.50", blocks)
        self.assertIn("2h 13m", blocks)
        self.assertNotIn("LLM errors", blocks)   # 0 -> omitted
        self.assertIn("Run ref: `r1`", blocks)
        # fallback line carries the gist for push notifications:
        self.assertIn("1,840 found / 160 not found", self._fallback())

    def test_complete_with_errors_uses_warning_headline_and_ok_failed(self):
        notify.notify_run_complete(pipeline="Google Maps", company="Acme Corp",
                                   run_ref="u1", status="completed_with_errors",
                                   success=90, failed=10, total_rows=100,
                                   duration_seconds=332, llm_errors=3)
        blocks = self._blocks_text()
        self.assertIn("Run completed with errors", blocks)
        self.assertIn("*90* succeeded", blocks)
        self.assertIn("*10* failed", blocks)
        self.assertIn("Status: `completed_with_errors`", blocks)
        self.assertIn("5m 32s", blocks)
        self.assertIn("LLM errors", blocks)

    def test_failed_message_with_detail(self):
        notify.notify_run_failed(pipeline="AI Mode 1 — Bulk", company="Acme Corp",
                                 run_ref="r1", error="SCRAPEDO_TOKEN missing",
                                 total_rows=2000, duration_seconds=42)
        blocks = self._blocks_text()
        self.assertIn("Run failed", blocks)
        self.assertIn("*AI Mode 1 — Bulk*", blocks)
        self.assertIn("Company: Acme Corp", blocks)
        self.assertIn("Status: `failed`", blocks)
        self.assertIn("SCRAPEDO_TOKEN missing", blocks)   # inside code block
        self.assertIn("Rows", blocks)
        self.assertIn("Ran for", blocks)
        self.assertIn("Run ref: `r1`", blocks)
        self.assertIn("Run failed", self._fallback())

    def test_missing_company_and_detail_degrade_gracefully(self):
        notify.notify_run_failed(pipeline="Google Search", company=None,
                                 run_ref="u1", error="x")
        blocks = self._blocks_text()
        self.assertIn("*Google Search*", blocks)
        self.assertIn("Company: —", blocks)
        self.assertIn("```x```", blocks)
        # no detail provided -> no field grid block emitted
        self.assertNotIn("Rows", blocks)
        self.assertNotIn("Ran for", blocks)


class PipelineLabelTests(unittest.TestCase):
    def test_known_keys_mapped(self):
        self.assertEqual(notify.pipeline_label("gmaps"), "Google Maps")
        self.assertEqual(notify.pipeline_label("ai_bulk"), "Google AI (Bulk)")

    def test_unknown_key_passthrough(self):
        self.assertEqual(notify.pipeline_label("weird"), "weird")
        self.assertEqual(notify.pipeline_label(None), "—")


if __name__ == "__main__":
    unittest.main()
