"""Offline tests for the firmographics scrape.do Search + deferred AI-Overview path.

No network: every call goes through an httpx MockTransport.
"""
import asyncio
import json
import os
import unittest
from unittest import mock

import httpx

from app.services.serpwow import scrapedo_search_client as client
from app.services.serpwow.modes.firmographics import (
    _cost_breakdown,
    execute_firmographic_extraction,
)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


SERP_COMPLETE = {
    "search_parameters": {"q": "..."},
    "ai_overview": {"state": "complete", "text_blocks": [{"snippet": "Acme is in Berlin"}]},
    "knowledge_graph": {"address": "Berlin"},
    "organic_results": [{"link": "https://acme.com"}],
}
SERP_DEFERRED = {
    "ai_overview": {"state": "deferred", "session_key": "SK123"},
}
SERP_NO_OVERVIEW = {"organic_results": [{"link": "https://acme.com"}]}


class SearchClientTests(unittest.TestCase):
    def setUp(self) -> None:
        # patch.dict restores on exit, so the token never outlives the test — the suite's
        # hermeticity guard asserts SCRAPEDO_TOKEN is blank in this process.
        self._env = mock.patch.dict(
            os.environ, {"SCRAPEDO_TOKEN": "test-token", "SCRAPEDO_MAX_RETRIES": "2"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_complete_overview_is_one_call_ten_credits(self) -> None:
        calls = []

        def handler(request):
            calls.append(str(request.url.path))
            return httpx.Response(200, json=SERP_COMPLETE)

        env = _run(client.search_with_ai_overview("q", "de", client=_client(handler)))
        self.assertEqual(calls, ["/plugin/google/search"])
        self.assertEqual(env["credits"], 10)
        self.assertEqual((env["search_successful"], env["ai_overview_successful"]), (1, 0))
        self.assertFalse(env["deferred"])
        self.assertEqual(env["ai_overview_state"], "complete")
        self.assertIsNone(env["error"])
        # The whole SERP is kept as the row's raw artifact, not just the overview.
        self.assertIn("organic_results", env["raw_response"])

    def test_deferred_triggers_five_credit_followup(self) -> None:
        calls = []

        def handler(request):
            calls.append(str(request.url.path))
            if request.url.path.endswith("/ai-overview"):
                self.assertEqual(request.url.params.get("session_key"), "SK123")
                return httpx.Response(200, json={
                    "ai_overview": {"state": "complete", "text_blocks": [{"snippet": "x"}]}})
            return httpx.Response(200, json=SERP_DEFERRED)

        env = _run(client.search_with_ai_overview("q", "us", client=_client(handler)))
        self.assertEqual(calls, ["/plugin/google/search",
                                 "/plugin/google/search/ai-overview"])
        # 10 for the search + 5 for the follow-up.
        self.assertEqual(env["credits"], 15)
        self.assertEqual(env["request_count"], 2)
        self.assertEqual(env["successful_requests"], 2)
        self.assertTrue(env["deferred"])
        # Resolved, so the row has an overview to normalise and is not "billed for
        # nothing".
        self.assertEqual(env["ai_overview_state"], "complete")
        self.assertFalse(env["billed_no_overview"])
        self.assertIsNone(env["ai_overview_error"])

    def test_expired_session_key_is_not_retried(self) -> None:
        """The key is single-use with a 60s expiry: a retry can only 404 again."""
        calls = []

        def handler(request):
            calls.append(str(request.url.path))
            if request.url.path.endswith("/ai-overview"):
                return httpx.Response(404, json={"error": "session not found"})
            return httpx.Response(200, json=SERP_DEFERRED)

        env = _run(client.search_with_ai_overview("q", "us", client=_client(handler)))
        self.assertEqual(calls.count("/plugin/google/search/ai-overview"), 1)
        # The failed follow-up is unbilled; only the search's 10 credits are charged.
        self.assertEqual(env["credits"], 10)
        self.assertEqual(env["ai_overview_successful"], 0)
        # The SEARCH succeeded, so this is not a row error — just no overview.
        self.assertIsNone(env["error"])
        self.assertIn("session not found", env["ai_overview_error"])
        self.assertTrue(env["billed_no_overview"])

    def test_deferred_without_session_key_makes_no_second_call(self) -> None:
        def handler(request):
            self.assertFalse(request.url.path.endswith("/ai-overview"))
            return httpx.Response(200, json={"ai_overview": {"state": "deferred"}})

        env = _run(client.search_with_ai_overview("q", "us", client=_client(handler)))
        self.assertEqual(env["ai_overview_requests"], 0)
        self.assertEqual(env["credits"], 10)
        self.assertIn("no session_key", env["ai_overview_error"])

    def test_no_overview_is_billed_but_empty_not_an_error(self) -> None:
        def handler(request):
            return httpx.Response(200, json=SERP_NO_OVERVIEW)

        env = _run(client.search_with_ai_overview("q", "us", client=_client(handler)))
        self.assertEqual(env["credits"], 10)
        self.assertIsNone(env["ai_overview_state"])
        self.assertIsNone(env["error"])
        self.assertTrue(env["billed_no_overview"])

    def test_5xx_retries_then_errors_unbilled(self) -> None:
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503, text="upstream down")

        env = _run(client.search_with_ai_overview("q", "us", client=_client(handler)))
        self.assertEqual(len(calls), 3)          # 1 attempt + SCRAPEDO_MAX_RETRIES=2
        self.assertEqual(env["search_requests"], 3)
        # Failed attempts are free: three calls, zero credits.
        self.assertEqual(env["credits"], 0)
        self.assertEqual(env["failed_requests"], 3)
        self.assertIsNotNone(env["error"])

    def test_token_is_never_leaked_into_an_error_string(self) -> None:
        def handler(request):
            return httpx.Response(400, json={
                "error": "bad request for https://api.scrape.do/x?token=test-token&q=z"})

        env = _run(client.search_with_ai_overview("q", "us", client=_client(handler)))
        self.assertIsNotNone(env["error"])
        self.assertNotIn("test-token", env["error"])
        self.assertIn("[REDACTED]", env["error"])

    def test_missing_token_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, {"SCRAPEDO_TOKEN": ""}):
            env = _run(client.search_with_ai_overview("q", "us"))
        self.assertEqual(env["error_category"], "auth")
        self.assertEqual(env["request_count"], 0)


class CostBreakdownTests(unittest.TestCase):
    def test_credits_split_by_endpoint(self) -> None:
        cb = _cost_breakdown({
            "search_requests": 1, "search_successful": 1,
            "ai_overview_requests": 1, "ai_overview_successful": 1,
            "request_count": 2, "successful_requests": 2,
            "credits": 15, "deferred": True,
        }, 0.0002)
        self.assertEqual(cb["scrapedo_credits"], 15)
        self.assertEqual(cb["scrapedo_search_successful"], 1)
        self.assertEqual(cb["scrapedo_ai_overview_successful"], 1)
        self.assertEqual(cb["scrapedo_ai_overview_deferred"], 1)
        # Credits are not dollars: the only USD is the LLM call.
        self.assertEqual(cb["total_cost_usd"], 0.0002)
        self.assertNotIn("serpwow_cost_usd", cb)
        self.assertNotIn("serpwow_request_count", cb)

    def test_failed_row_records_error_requests(self) -> None:
        cb = _cost_breakdown({
            "search_requests": 3, "search_successful": 0,
            "request_count": 3, "successful_requests": 0,
            "credits": 0, "error": "boom",
        }, 0.0)
        self.assertEqual(cb["scrapedo_error_requests"], 3)
        self.assertEqual(cb["scrapedo_credits"], 0)
        self.assertEqual(cb["scrapedo_billed_errors"], 0)   # never saw a 200


class ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        # GEMINI_API_KEY blank keeps the LLM step offline: the normaliser returns
        # ("GEMINI_API_KEY not configured") without touching the network.
        self._env = mock.patch.dict(
            os.environ, {"SCRAPEDO_TOKEN": "test-token", "GEMINI_API_KEY": ""})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_row_carries_credits_and_no_provider_usd(self) -> None:
        async def fake_search(website, country=None):
            return {"provider": "scrapedo", "used": True, "domain": "acme.com",
                    "query": "q", "ai_overview": {"state": "complete", "x": 1},
                    "ai_overview_state": "complete", "deferred": True,
                    "billed_no_overview": False, "raw_response": SERP_COMPLETE,
                    "search_requests": 1, "search_successful": 1,
                    "ai_overview_requests": 1, "ai_overview_successful": 1,
                    "request_count": 2, "successful_requests": 2,
                    "failed_requests": 0, "credits": 15, "error": None}

        with mock.patch(
            "app.services.serpwow.modes.firmographics.run_scrapedo_search_for_firmographics",
            new=fake_search,
        ):
            resp, raw = _run(execute_firmographic_extraction(
                "acme.com", "Acme", "Germany"))
        cb = resp.context["cost_breakdown"]
        self.assertEqual(cb["scrapedo_credits"], 15)
        # "Not applicable", not a misleading $0.00 — the gmaps convention.
        self.assertIsNone(resp.serpwow_cost_usd)
        self.assertIsNone(resp.massive_proxy_cost_usd)
        self.assertEqual(resp.total_cost_usd, resp.gemini_cost_usd)
        # The raw artifact is the whole SERP, so a stored row can be re-judged without
        # re-buying the call.
        self.assertIn("organic_results", json.loads(raw))

    def test_provider_error_becomes_a_row_error_not_a_silent_empty(self) -> None:
        async def fake_search(website, country=None):
            return {"provider": "scrapedo", "used": False, "domain": "acme.com",
                    "query": "q", "ai_overview": None, "ai_overview_state": None,
                    "deferred": False, "billed_no_overview": False, "raw_response": None,
                    "search_requests": 3, "search_successful": 0,
                    "ai_overview_requests": 0, "ai_overview_successful": 0,
                    "request_count": 3, "successful_requests": 0, "failed_requests": 3,
                    "credits": 0, "error": "scrape.do search failed (HTTP 429).",
                    "error_category": "rate_limit"}

        with mock.patch(
            "app.services.serpwow.modes.firmographics.run_scrapedo_search_for_firmographics",
            new=fake_search,
        ):
            resp, _ = _run(execute_firmographic_extraction("acme.com", "Acme", "DE"))
        # The structure outcomes._phase_stats reads, tagged with the right provider, so
        # the row classifies as error/scrapedo instead of "no firmographics found".
        phases = resp.context["formatted_results"]
        self.assertEqual(len(phases), 1)
        self.assertFalse(phases[0]["success"])
        self.assertEqual(phases[0]["error_source"], "scrapedo")
        self.assertEqual(phases[0]["error_category"], "rate_limit")

    def test_invalid_website_is_rejected_before_any_spend(self) -> None:
        resp, raw = _run(execute_firmographic_extraction(
            "https://www.linkedin.com/company/acme", "Acme", "DE"))
        self.assertIsNone(resp.official_website)
        self.assertEqual(resp.context["cost_breakdown"]["scrapedo_requests"], 0)
        self.assertEqual(raw, "")


class SummaryChipTests(unittest.TestCase):
    """What the run-detail header must be able to say about a firmographics run.

    It reports confidence_mode=None (it is handed the website, so there is nothing to be
    confident about), which hid the fact that a paid model ran at all — and whether it ran
    batched. llm_mode/model/phase_seconds_avg are what the chips read instead.
    """

    def _state(self, rows):
        return {"upload_id": "UP", "company_name": "T", "pipeline": "firmographics",
                "status": "completed", "total_rows": len(rows), "rows": rows}

    def _row(self, search=1.0, llm=0.5, model="gemini-2.5-flash-lite"):
        return {"row_index": 1, "status": "completed", "outcome": "found",
                "result": {"official_website": "https://a.com", "address": "X",
                           "gemini_cost_usd": 0.0002,
                           "context": {
                               "timing": {"search_seconds": search, "llm_seconds": llm,
                                          "total_seconds": search + llm},
                               "mapping_ai": {"model": model,
                                              "usage": {"promptTokenCount": 900,
                                                        "candidatesTokenCount": 200}},
                               "cost_breakdown": {"scrapedo_requests": 1,
                                                  "scrapedo_successful_requests": 1,
                                                  "scrapedo_credits": 10,
                                                  "scrapedo_search_successful": 1}}}}

    def test_llm_mode_is_inline_and_model_is_reported(self) -> None:
        from app.services.serpwow import reporting as rep
        state = self._state([self._row()])
        s = rep.build_summary(state, rep.state_to_entity_results(state))
        # Gemini batch is gsearch-only machinery; this pipeline calls it once per row.
        self.assertEqual(s["llm_mode"], "inline")
        self.assertEqual(s["model"], "gemini-2.5-flash-lite")
        self.assertIsNone(s["confidence_mode"])
        self.assertEqual(s["token_usage"]["total_tokens"], 1100)

    def test_phase_time_is_a_per_row_average_not_a_sum(self) -> None:
        """Rows run concurrently, so a SUM would exceed the run's own wall clock."""
        from app.services.serpwow import reporting as rep
        rows = [self._row(search=2.0, llm=1.0), self._row(search=4.0, llm=3.0)]
        state = self._state(rows)
        s = rep.build_summary(state, rep.state_to_entity_results(state))
        self.assertEqual(s["phase_seconds_avg"], {"provider": 3.0, "llm": 2.0})

    def test_pipelines_without_the_split_grow_no_key(self) -> None:
        from app.services.serpwow import reporting as rep
        state = {"upload_id": "UP", "pipeline": "gsearch", "status": "completed", "rows": [
            {"status": "completed", "outcome": "found",
             "result": {"official_website": "https://a.com", "context": {}}}]}
        s = rep.build_summary(state, rep.state_to_entity_results(state))
        self.assertNotIn("phase_seconds_avg", s)


if __name__ == "__main__":
    unittest.main()
