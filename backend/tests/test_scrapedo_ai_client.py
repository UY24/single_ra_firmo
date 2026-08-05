"""scrape.do Google AI Mode client — the relationship pipeline's provider.

Drives the real client through httpx.MockTransport (no network) using a payload
shaped like a live /plugin/google/search/ai-mode response.
"""
import asyncio
import os
import unittest
from unittest import mock

import httpx

from app.services.serpwow import scrapedo_ai_client as client

TOKEN_ENV = {"SCRAPEDO_TOKEN": "test-token", "SCRAPEDO_MAX_RETRIES": "3",
             "SCRAPEDO_TIMEOUT_SECONDS": "5"}

PAYLOAD = {
    "search_parameters": {"q": "prompt text", "hl": "en", "gl": "us"},
    "text_blocks": [
        {"type": "paragraph", "snippet": "Acme Capital led a Series B in Sanzo.",
         "level": 1},
        {"type": "paragraph", "snippet": "Sanzo's website is https://drinksanzo.com",
         "level": 1},
    ],
    "references": [
        {"title": "Sanzo", "link": "https://drinksanzo.com",
         "snippet": "Official site", "source": "drinksanzo.com", "index": 1},
    ],
}


def _run(handler, query="prompt text", **env):
    """Run search_ai_mode against a mock transport; returns the envelope."""
    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            return await client.search_ai_mode(query, gl="us", client=http_client)

    with mock.patch.dict(os.environ, {**TOKEN_ENV, **env}, clear=False):
        return asyncio.run(go())


class SuccessTests(unittest.TestCase):
    def test_payload_fields_pass_through_verbatim(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=PAYLOAD)

        env = _run(handler)

        # The WHOLE provider body, byte-for-byte — raw/ is the only copy of it, so a
        # selection of fields would make the artifact useless for judging the prompt.
        self.assertEqual(env["response"], PAYLOAD)
        self.assertIsNone(env["error"])
        self.assertIn("token=test-token", seen["url"])
        self.assertIn("gl=us", seen["url"])

    def test_one_successful_call_costs_ten_credits(self) -> None:
        env = _run(lambda request: httpx.Response(200, json=PAYLOAD))
        self.assertEqual(env["request_count"], 1)
        self.assertEqual(env["successful_requests"], 1)
        self.assertEqual(env["failed_requests"], 0)
        self.assertEqual(env["credits"], 10)
        self.assertFalse(env["billed_empty"])

    def test_empty_200_is_billed_empty_not_an_error(self) -> None:
        env = _run(lambda request: httpx.Response(
            200, json={"text_blocks": [], "references": []}))
        self.assertIsNone(env["error"])
        self.assertEqual(env["credits"], 10)
        self.assertTrue(env["billed_empty"])


class RetryTests(unittest.TestCase):
    def test_529_is_retried_four_times_total_then_errors(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(529, json={"error": "overloaded"})

        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            env = _run(handler)

        # SCRAPEDO_MAX_RETRIES=3 means 1 attempt + 3 retries.
        self.assertEqual(calls["n"], 4)
        self.assertEqual(env["request_count"], 4)
        self.assertEqual(env["successful_requests"], 0)
        self.assertEqual(env["credits"], 0)  # failures are free
        self.assertIsNotNone(env["error"])

    def test_recovers_on_the_third_attempt_and_bills_once(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(502, json={"error": "request failed"})
            return httpx.Response(200, json=PAYLOAD)

        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            env = _run(handler)

        self.assertEqual(env["request_count"], 3)
        self.assertEqual(env["successful_requests"], 1)
        self.assertEqual(env["failed_requests"], 2)
        self.assertEqual(env["credits"], 10)
        self.assertIsNone(env["error"])

    def test_4xx_is_not_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401, json={"error": "bad token"})

        env = _run(handler)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(env["error_category"], "auth")


class SafetyTests(unittest.TestCase):
    def test_token_is_redacted_from_errors(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text=f"failed for {request.url}")

        env = _run(handler)
        self.assertNotIn("test-token", env["error"])
        self.assertIn("REDACTED", env["error"])

    def test_errors_name_the_ai_mode_endpoint_not_google_maps(self) -> None:
        """This string is persisted per row and shown in "View failed rows". Reusing the
        gmaps helper's hardcoded text reported every transport error, 429 and 5xx on this
        pipeline as a Google MAPS failure."""
        env = _run(lambda request: httpx.Response(500, json={"error": "upstream boom"}),
                   SCRAPEDO_MAX_RETRIES="0")
        self.assertIn("ai-mode", env["error"])
        self.assertNotIn("maps", env["error"])

    def test_a_json_error_body_is_redacted_too(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": f"bad token for {request.url}"})

        env = _run(handler, SCRAPEDO_MAX_RETRIES="0")
        self.assertNotIn("test-token", env["error"])
        self.assertIn("[REDACTED]", env["error"])

    def test_missing_token_errors_without_calling_out(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make a request without a token")

        env = _run(handler, SCRAPEDO_TOKEN="")
        self.assertEqual(env["error_category"], "auth")
        self.assertEqual(env["request_count"], 0)


if __name__ == "__main__":
    unittest.main()
