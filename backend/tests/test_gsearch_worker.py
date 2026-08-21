import asyncio
import unittest
from unittest import mock

from app.services.serpwow import engine as legacy_app
# execute_gsearch_lookup_for_worker now lives in modes.gsearch and resolves
# run_serpwow_search / choose_final_website_with_gemini in that module's namespace.
from app.services.serpwow.modes import gsearch as gsearch_mode


def _fake_serpwow(query, country=None, client=None):
    async def _coro():
        return {
            "provider": "serpwow", "used": True, "query": query,
            "official_website": "https://acme-motors.com",
            "candidates": ["https://acme-motors.com", "https://acme-parts.com"],
            "status_code": 200, "search_url": "http://serp/u", "raw_response": {},
            "error": None,
        }
    return _coro()


class TestGsearchWorker(unittest.TestCase):
    def test_confidence_populated_and_website_selected(self):
        confidence_raw = {"official_website": "https://acme-motors.com",
                          "confidence_score": 91, "confidence": "high",
                          "reason": "match", "evidence": [], "alternatives": []}
        with mock.patch.object(gsearch_mode, "run_serpwow_search", _fake_serpwow), \
             mock.patch.object(gsearch_mode, "choose_final_website_with_gemini",
                               return_value=(confidence_raw, None, "gemini-2.5-flash-lite",
                                             {"promptTokenCount": 50, "candidatesTokenCount": 10})), \
             mock.patch.dict("os.environ", {"LLM_BATCH": "false",
                                            "ENABLE_FINAL_URL_GEMINI": "true",
                                            "GEMINI_API_KEY": "k"}):
            resp, raw = asyncio.run(legacy_app.execute_gsearch_lookup_for_worker(
                company_name="Acme Motors", country="us", phase="phase1"))
        ctx = resp.context
        self.assertEqual(resp.official_website, "https://acme-motors.com")
        self.assertTrue(ctx["final_url_selection_ai"]["used"])
        self.assertEqual(ctx["final_url_selection_ai"]["raw"]["confidence_score"], 91)
        self.assertGreater(resp.gemini_cost_usd, 0.0)
        self.assertFalse(ctx["skip_llm"])

    def test_failed_serpwow_attempts_are_not_billed(self):
        async def failed_search(query, country=None, client=None):
            return {"provider": "serpwow", "used": False, "query": query,
                    "official_website": None, "candidates": [],
                    "status_code": 502, "search_url": None, "raw_response": None,
                    "error": "SerpWow failed (HTTP 502).",
                    "error_category": "http_5xx"}

        with mock.patch.object(gsearch_mode, "run_serpwow_search", failed_search), \
             mock.patch.object(gsearch_mode, "choose_final_website_with_gemini") as chooser, \
             mock.patch.dict("os.environ", {
                 "LLM_BATCH": "false",
                 "SERPWOW_USD_PER_SEARCH": "0.00035",
             }):
            resp, _ = asyncio.run(legacy_app.execute_gsearch_lookup_for_worker(
                company_name="Acme Motors", country="us", phase="phase1"))

        self.assertGreater(resp.context["cost_breakdown"]["serpwow_request_count"], 0)
        self.assertEqual(
            resp.context["cost_breakdown"]["serpwow_billable_request_count"], 0)
        self.assertEqual(resp.serpwow_cost_usd, 0.0)
        self.assertEqual(resp.context["candidates"], [])
        self.assertTrue(resp.context["skip_llm"])
        chooser.assert_not_called()

    def test_successful_search_without_candidates_skips_llm_as_not_found(self):
        async def no_result(query, country=None, client=None):
            return {"provider": "serpwow", "used": True, "query": query,
                    "official_website": None, "candidates": [],
                    "status_code": 200, "search_url": "https://search.example",
                    "raw_response": {}, "error": None, "error_category": None}

        with mock.patch.object(gsearch_mode, "run_serpwow_search", no_result), \
             mock.patch.object(gsearch_mode, "choose_final_website_with_gemini") as chooser, \
             mock.patch.dict("os.environ", {"LLM_BATCH": "false"}):
            resp, _ = asyncio.run(legacy_app.execute_gsearch_lookup_for_worker(
                company_name="Acme Motors", country="us", phase="phase1"))

        chooser.assert_not_called()
        self.assertTrue(resp.context["skip_llm"])
        from app.services.serpwow import outcomes as o
        info = o.classify_finalized_row(
            {"official_website": None, "context": resp.context},
            pipeline="gsearch", ctx_row_error=None, skip_llm=True)
        self.assertEqual(info.outcome, o.OUTCOME_NOT_FOUND)

    def test_per_row_gemini_failure_produces_degraded_found(self):
        # PRODUCER-level test: the real gsearch worker, when the per-row Gemini
        # selection genuinely fails (output=None + error), must STILL emit a website
        # (the raw first candidate fallback) AND record context["llm_error"]. Feeding
        # that result to the classifier must yield found + degraded_search (not error).
        from app.services.serpwow import outcomes as o
        with mock.patch.object(gsearch_mode, "run_serpwow_search", _fake_serpwow), \
             mock.patch.object(gsearch_mode, "choose_final_website_with_gemini",
                               return_value=(None, "Gemini HTTPError: 429",
                                             "gemini-2.5-flash-lite", None)), \
             mock.patch.dict("os.environ", {"LLM_BATCH": "false",
                                            "ENABLE_FINAL_URL_GEMINI": "true",
                                            "GEMINI_API_KEY": "k"}):
            resp, raw = asyncio.run(legacy_app.execute_gsearch_lookup_for_worker(
                company_name="Acme Motors", country="us", phase="phase1"))
        # Candidate fallback keeps the row found despite the Gemini failure.
        self.assertEqual(resp.official_website, "https://acme-motors.com")
        self.assertEqual(resp.context["llm_error"], "Gemini HTTPError: 429")
        info = o.classify_finalized_row(
            {"official_website": resp.official_website, "context": resp.context},
            pipeline="gsearch", ctx_row_error=None, skip_llm=False)
        self.assertEqual(info.outcome, o.OUTCOME_FOUND)
        self.assertTrue(info.degraded_search)

    def test_batch_mode_skips_per_row_llm(self):
        with mock.patch.object(gsearch_mode, "run_serpwow_search", _fake_serpwow), \
             mock.patch.object(gsearch_mode, "choose_final_website_with_gemini") as chooser, \
             mock.patch.dict("os.environ", {"LLM_BATCH": "true"}):
            resp, raw = asyncio.run(legacy_app.execute_gsearch_lookup_for_worker(
                company_name="Acme Motors", country="us", phase="phase1"))
        chooser.assert_not_called()
        self.assertFalse(resp.context["final_url_selection_ai"]["used"])
