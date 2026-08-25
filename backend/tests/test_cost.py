# backend/tests/test_cost.py
"""Tests for per-run cost summary — LLM USD + scrape.do search count.

scrape.do is a flat fee, so there is no scrape.do dollar cost; the summary only counts
searches done (one scrape.do request = one search)."""
import os
import unittest
from unittest import mock

from app.services.ai_mode.cost import (
    build_cost_summary,
    calculate_llm_cost_usd,
)


class TestBuildCostSummary(unittest.TestCase):
    def test_counts_searches_and_total_is_llm_only(self):
        c = build_cost_summary(llm_usd=1.5, request_count=2)
        self.assertEqual(c["scrapedo_searches"], 2)
        self.assertEqual(c["llm_usd"], 1.5)
        self.assertEqual(c["total_usd"], 1.5)
        self.assertNotIn("scrapedo_usd", c)
        self.assertNotIn("scrapedo_credits", c)

    def test_zero_requests(self):
        c = build_cost_summary(llm_usd=0.5, request_count=0)
        self.assertEqual(c["scrapedo_searches"], 0)
        self.assertEqual(c["total_usd"], 0.5)


class TestCalculateLlmCostUsd(unittest.TestCase):
    GEMINI_PRICING = {
        "GEMINI_INPUT_USD_PER_1M_TOKENS": "0.10",
        "GEMINI_OUTPUT_USD_PER_1M_TOKENS": "0.40",
        "GEMINI_BATCH_INPUT_USD_PER_1M_TOKENS": "0.05",
        "GEMINI_BATCH_OUTPUT_USD_PER_1M_TOKENS": "0.20",
    }

    def test_gemini_sync_pricing(self):
        with mock.patch.dict(os.environ, self.GEMINI_PRICING):
            cost = calculate_llm_cost_usd(
                prompt_tokens=1_000_000, completion_tokens=1_000_000, batch_mode=False,
            )
        self.assertAlmostEqual(cost, 0.50)

    def test_gemini_batch_pricing_uses_batch_rates(self):
        with mock.patch.dict(os.environ, self.GEMINI_PRICING):
            cost = calculate_llm_cost_usd(
                prompt_tokens=1_000_000, completion_tokens=1_000_000, batch_mode=True,
            )
        self.assertAlmostEqual(cost, 0.25)


class TestScrapeDoClientReturnsPayload(unittest.TestCase):
    """search_google_ai_mode returns just the parsed payload (no cost tuple)."""

    def _fake_httpx_client(self):
        import httpx

        response = mock.MagicMock()
        response.status_code = 200
        response.headers = httpx.Headers({})
        response.text = '{"text_blocks": []}'
        response.json.return_value = {"text_blocks": [], "references": []}
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.get.return_value = response
        return client

    def test_returns_payload_only(self):
        from app.services.ai_mode.scrapedo_client import ScrapeDoClient

        fake = self._fake_httpx_client()
        with mock.patch("app.services.ai_mode.scrapedo_client.httpx.Client",
                        return_value=fake):
            payload = ScrapeDoClient(token="t").search_google_ai_mode("query")
        self.assertEqual(payload, {"text_blocks": [], "references": []})


if __name__ == "__main__":
    unittest.main()
