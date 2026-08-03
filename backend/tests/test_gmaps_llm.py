import asyncio
import unittest
from unittest import mock

from app.services.serpwow import engine as legacy_app
# execute_gmaps_lookup now lives in modes.gmaps and resolves run_gmaps_from_module /
# choose_final_website_with_gemini in that module's namespace, so mocks patch there.
from app.services.serpwow.modes import gmaps as gmaps_mode


def _gmaps_ctx(website="https://acme-motors.com"):
    return {
        "provider": "scrapedo", "used": True, "query": "Acme Motors",
        "official_website": website, "request_count": 1,
        "successful_requests": 1, "failed_requests": 0, "credits": 10,
        "no_results": False, "error": None, "error_category": None,
        "raw_response": {"results": [
            {"title": "Acme Motors", "website": website, "address": "500 Main Street",
             "type": "Car dealer", "types": ["Car dealer", "Used car dealer"]}]},
    }


def _run(ctx=None, **env):
    """Run execute_gmaps_lookup with run_gmaps_from_module mocked; env overrides applied."""
    with mock.patch.object(gmaps_mode, "run_gmaps_from_module",
                           new=mock.AsyncMock(return_value=ctx or _gmaps_ctx())), \
         mock.patch.dict("os.environ", env, clear=False):
        resp, _ = asyncio.run(legacy_app.execute_gmaps_lookup(
            "Acme Motors", "us", input_full_address="500 Main Street"))
    return resp


class TestGmapsPerRowLLM(unittest.TestCase):
    def test_llm_success_writes_final_url_selection_ai(self):
        raw = {"official_website": "https://acme-motors.com", "confidence_score": 92,
               "confidence": "high", "reason": "match", "evidence": [], "alternatives": []}
        selector = mock.MagicMock(return_value=(raw, None, "gemini-2.5-flash-lite",
                                                {"promptTokenCount": 50, "candidatesTokenCount": 10}))
        with mock.patch.object(gmaps_mode, "choose_final_website_with_gemini", selector):
            resp = _run(GMAPS_CONFIDENCE_MODE="llm", GMAPS_LLM_BATCH="false")
        ctx = resp.context
        self.assertIn("candidates", ctx)
        self.assertEqual(ctx["final_url_selection_ai"]["raw"], raw)
        self.assertEqual(ctx["final_url_selection_ai"]["model"], "gemini-2.5-flash-lite")
        self.assertEqual(resp.official_website, "https://acme-motors.com")
        self.assertGreater(resp.gemini_cost_usd, 0.0)
        self.assertNotIn("gmaps_confidence", ctx)  # LLM path supersedes heuristic
        selector.assert_called_once()

    def test_llm_error_falls_back_to_heuristic(self):
        selector = mock.MagicMock(return_value=(None, "boom", "gemini-2.5-flash-lite", None))
        with mock.patch.object(gmaps_mode, "choose_final_website_with_gemini", selector):
            resp = _run(GMAPS_CONFIDENCE_MODE="llm", GMAPS_LLM_BATCH="false")
        ctx = resp.context
        self.assertIn("gmaps_confidence", ctx)
        self.assertTrue(ctx["gmaps_confidence"]["mode"].startswith("llm (fallback"))
        self.assertEqual(resp.official_website, "https://acme-motors.com")  # kept Python pick
        self.assertEqual(resp.gemini_cost_usd, 0.0)


class TestGmapsBatchCandidates(unittest.TestCase):
    def test_batch_mode_sets_candidates_no_perrow_call(self):
        selector = mock.MagicMock()
        with mock.patch.object(gmaps_mode, "choose_final_website_with_gemini", selector):
            resp = _run(GMAPS_CONFIDENCE_MODE="llm", GMAPS_LLM_BATCH="true")
        ctx = resp.context
        self.assertIn("candidates", ctx)
        self.assertIn("gmaps_confidence", ctx)          # heuristic placeholder pre-batch
        self.assertNotIn("final_url_selection_ai", ctx)  # batch decides at finalization
        selector.assert_not_called()


class TestGmapsHeuristicUnchanged(unittest.TestCase):
    def test_heuristic_default_no_llm_call(self):
        selector = mock.MagicMock()
        with mock.patch.object(gmaps_mode, "choose_final_website_with_gemini", selector):
            resp = _run()  # no env -> heuristic
        ctx = resp.context
        self.assertIn("gmaps_confidence", ctx)
        self.assertEqual(ctx["gmaps_confidence"]["mode"], "heuristic")
        self.assertNotIn("final_url_selection_ai", ctx)
        selector.assert_not_called()
        self.assertEqual(resp.gemini_cost_usd, 0.0)


class TestGmapsScrapedoBilling(unittest.TestCase):
    """gmaps moved to scrape.do (credits) from SerpWow (per-search USD) in 2026-08."""

    def test_cost_breakdown_reports_credits_and_no_serpwow_keys_at_all(self):
        cb = _run().context["cost_breakdown"]
        self.assertEqual(cb["scrapedo_requests"], 1)
        self.assertEqual(cb["scrapedo_credits"], 10)
        # gmaps left SerpWow entirely — no vestigial zeroed keys.
        self.assertEqual([k for k in cb if k.startswith("serpwow")], [])
        self.assertNotIn("massive_proxy_cost_usd", cb)

    def test_serpwow_rate_cannot_leak_into_a_scrapedo_run(self):
        # Even with a SerpWow rate configured, a gmaps run reports no SerpWow spend.
        resp = _run(SERPWOW_USD_PER_SEARCH="0.00035")
        self.assertIsNone(resp.serpwow_cost_usd)
        self.assertIsNone(resp.massive_proxy_cost_usd)
        self.assertEqual(resp.total_cost_usd, resp.gemini_cost_usd)

    def test_industry_comes_from_scrapedo_types(self):
        # scrape.do has no `categories`/`category` key — only `type`/`types`.
        self.assertEqual(_run().industry, "Car dealer")


class TestGmapsProviderErrorIsAnError(unittest.TestCase):
    """A scrape.do failure must not masquerade as a business "no website found":
    that would hide the failure from the error breakdown AND leave the row
    unreachable by "Rerun failed"."""

    FAILED_CTX: dict = {
        "provider": "scrapedo", "used": False, "query": "Acme Motors",
        "official_website": None, "request_count": 4,
        "successful_requests": 0, "failed_requests": 4, "credits": 0,
        "no_results": False, "raw_response": {"results": []},
        "error": "scrape.do maps search failed (HTTP 429): rate limited.",
        "error_category": "rate_limit",
    }

    def test_row_classifies_as_error_attributed_to_scrapedo(self):
        resp = _run(ctx=self.FAILED_CTX)
        info = legacy_app._outcomes.classify_finalized_row(
            resp.model_dump(), pipeline="gmaps", ctx_row_error=None, skip_llm=False)
        self.assertEqual(info.outcome, "error")
        self.assertEqual(info.error_source, "scrapedo")
        self.assertEqual(info.error_category, "rate_limit")
        self.assertIn("429", info.error_detail)

    def test_failed_call_is_not_billed(self):
        cb = _run(ctx=self.FAILED_CTX).context["cost_breakdown"]
        self.assertEqual(cb["scrapedo_credits"], 0)
        # All 4 attempts (1 + 3 retries) are visible, none of them billed.
        self.assertEqual(cb["scrapedo_requests"], 4)
        self.assertEqual(cb["scrapedo_failed_requests"], 4)
        self.assertEqual(cb["scrapedo_successful_requests"], 0)

    def test_successful_row_is_not_an_error(self):
        resp = _run()
        info = legacy_app._outcomes.classify_finalized_row(
            resp.model_dump(), pipeline="gmaps", ctx_row_error=None, skip_llm=False)
        self.assertEqual(info.outcome, "found")

    def test_no_maps_listing_is_not_found_not_an_error(self):
        """scrape.do's 502 "no results" = Google has no listing. 12/100 rows of a live
        run hit this and were wrongly reported as errors, which inflated the error
        count and made them eligible for a "Rerun failed" that can never succeed."""
        ctx = {
            "provider": "scrapedo", "used": True, "query": "Acme Motors",
            "official_website": None, "request_count": 1,
            "successful_requests": 0, "failed_requests": 1, "credits": 0,
            "no_results": True, "raw_response": {"results": []},
            "error": None, "error_category": None,
        }
        resp = _run(ctx=ctx)
        info = legacy_app._outcomes.classify_finalized_row(
            resp.model_dump(), pipeline="gmaps",
            ctx_row_error=resp.context.get("row_error"), skip_llm=False)
        self.assertEqual(info.outcome, "not_found")
        self.assertIsNone(info.error_source)
        self.assertEqual(resp.context["row_error"],
                         "No Google Maps listing exists for this company.")
        self.assertIn("No Google Maps listing", resp.summary)
        # Attempted but unbilled: visible in the call accounting, costing nothing.
        cb = resp.context["cost_breakdown"]
        self.assertEqual(cb["scrapedo_credits"], 0)
        self.assertEqual(cb["scrapedo_failed_requests"], 1)

    def test_call_accounting_reconciles(self):
        cb = _run().context["cost_breakdown"]
        self.assertEqual(
            cb["scrapedo_requests"],
            cb["scrapedo_successful_requests"] + cb["scrapedo_failed_requests"])
        self.assertEqual(cb["scrapedo_credits"], 10 * cb["scrapedo_successful_requests"])

    def test_genuine_no_result_is_still_not_found_not_an_error(self):
        ctx = _gmaps_ctx(website=None)
        ctx["raw_response"] = {"results": []}
        resp = _run(ctx=ctx)
        info = legacy_app._outcomes.classify_finalized_row(
            resp.model_dump(), pipeline="gmaps", ctx_row_error=None, skip_llm=False)
        self.assertEqual(info.outcome, "not_found")
