# backend/tests/test_cost.py
"""Tests for per-run cost summary (LLM + scrape.do) — impplan Task 15."""
import os
import unittest
from unittest import mock

from app.services.ai_mode.cost import (
    build_cost_summary,
    calculate_llm_cost_usd,
    extract_scrapedo_request_cost,
)


class TestScrapedoCost(unittest.TestCase):
    def test_extracts_header_cost(self):
        headers = {"Scrape.do-Request-Cost": "5"}
        self.assertEqual(extract_scrapedo_request_cost(headers), 5.0)

    def test_missing_header_returns_none(self):
        self.assertIsNone(extract_scrapedo_request_cost({}))

    def test_non_numeric_header_returns_none(self):
        self.assertIsNone(extract_scrapedo_request_cost({"Scrape.do-Request-Cost": "abc"}))

    def test_alternate_header_candidate(self):
        self.assertEqual(extract_scrapedo_request_cost({"sd-request-cost": "2.5"}), 2.5)


class TestBuildCostSummary(unittest.TestCase):
    def test_headers_present_sums_credits(self):
        with mock.patch.dict(os.environ, {"SCRAPEDO_COST_PER_REQUEST_USD": "0.002"}):
            c = build_cost_summary(llm_usd=1.5, request_costs=[5.0, 5.0], request_count=2)
        self.assertEqual(c["scrapedo_requests"], 2)
        self.assertEqual(c["scrapedo_credits"], 10.0)
        self.assertAlmostEqual(c["total_usd"], 1.5 + c["scrapedo_usd"])

    def test_fallback_estimate_when_no_headers(self):
        with mock.patch.dict(os.environ, {"SCRAPEDO_COST_PER_REQUEST_USD": "0.002"}):
            c = build_cost_summary(llm_usd=1.0, request_costs=[None, None], request_count=2)
        self.assertAlmostEqual(c["scrapedo_usd"], 0.004)
        self.assertTrue(c["scrapedo_cost_estimated"])

    def test_partial_headers_marks_estimated_but_keeps_known_credits(self):
        with mock.patch.dict(os.environ, {"SCRAPEDO_COST_PER_REQUEST_USD": "0.002"}):
            c = build_cost_summary(llm_usd=0.0, request_costs=[5.0, None], request_count=2)
        self.assertEqual(c["scrapedo_credits"], 5.0)
        self.assertTrue(c["scrapedo_cost_estimated"])
        self.assertAlmostEqual(c["scrapedo_usd"], 0.004)

    def test_zero_requests_and_no_env_rate(self):
        with mock.patch.dict(os.environ, {"SCRAPEDO_COST_PER_REQUEST_USD": ""}):
            c = build_cost_summary(llm_usd=0.5, request_costs=[], request_count=0)
        self.assertEqual(c["scrapedo_usd"], 0.0)
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
                provider="gemini", prompt_tokens=1_000_000,
                completion_tokens=1_000_000, batch_mode=False,
            )
        self.assertAlmostEqual(cost, 0.50)

    def test_gemini_batch_pricing_uses_batch_rates(self):
        with mock.patch.dict(os.environ, self.GEMINI_PRICING):
            cost = calculate_llm_cost_usd(
                provider="gemini", prompt_tokens=1_000_000,
                completion_tokens=1_000_000, batch_mode=True,
            )
        self.assertAlmostEqual(cost, 0.25)

    def test_openai_pricing_from_env(self):
        with mock.patch.dict(os.environ, {
            "OPENAI_INPUT_USD_PER_1M_TOKENS": "0.15",
            "OPENAI_OUTPUT_USD_PER_1M_TOKENS": "0.60",
        }):
            cost = calculate_llm_cost_usd(
                provider="openai", prompt_tokens=1_000_000, completion_tokens=500_000,
            )
        self.assertAlmostEqual(cost, 0.15 + 0.30)

    def test_openai_defaults_to_zero_without_env(self):
        with mock.patch.dict(os.environ, {
            "OPENAI_INPUT_USD_PER_1M_TOKENS": "",
            "OPENAI_OUTPUT_USD_PER_1M_TOKENS": "",
        }):
            cost = calculate_llm_cost_usd(
                provider="openai", prompt_tokens=1_000_000, completion_tokens=1_000_000,
            )
        self.assertEqual(cost, 0.0)


class TestScrapeDoClientCostCapture(unittest.TestCase):
    """search_google_ai_mode returns (payload, request_cost)."""

    def _fake_httpx_client(self, headers):
        import httpx

        response = mock.MagicMock()
        response.status_code = 200
        response.headers = httpx.Headers(headers)
        response.text = '{"text_blocks": []}'
        response.json.return_value = {"text_blocks": [], "references": []}
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.get.return_value = response
        return client

    def test_returns_payload_and_cost_from_header(self):
        from app.services.ai_mode.scrapedo_client import ScrapeDoClient

        fake = self._fake_httpx_client({"scrape.do-request-cost": "5"})
        with mock.patch("app.services.ai_mode.scrapedo_client.httpx.Client",
                        return_value=fake):
            payload, cost = ScrapeDoClient(token="t").search_google_ai_mode("query")
        self.assertEqual(payload, {"text_blocks": [], "references": []})
        self.assertEqual(cost, 5.0)

    def test_returns_none_cost_when_header_missing(self):
        from app.services.ai_mode.scrapedo_client import ScrapeDoClient

        fake = self._fake_httpx_client({})
        logs: list[str] = []
        with mock.patch("app.services.ai_mode.scrapedo_client.httpx.Client",
                        return_value=fake):
            payload, cost = ScrapeDoClient(token="t", log=logs.append).search_google_ai_mode("query")
        self.assertIsNone(cost)
        self.assertEqual(payload, {"text_blocks": [], "references": []})
        # run.log must make the real cost header discoverable on a live run
        # (header names only — no values that could leak secrets).
        self.assertTrue(any("response_headers=" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
