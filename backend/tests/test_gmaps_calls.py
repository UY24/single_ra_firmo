import asyncio
import unittest
from unittest import mock

from app.services.serpwow import engine as legacy_app
# execute_gmaps_lookup lives in modes.gmaps and resolves run_gmaps_from_module in that
# module's namespace, so mocks patch there.
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


class TestGmapsHeuristicOnly(unittest.TestCase):
    """The LLM confidence modes were deleted with the move to the S3-only runner —
    heuristic is the only path, so a row always carries a gmaps_confidence block and
    never a gsearch-shaped final_url_selection_ai one."""

    def test_confidence_is_always_the_heuristic_block(self):
        ctx = _run().context
        self.assertEqual(ctx["gmaps_confidence"]["mode"], "heuristic")
        self.assertNotIn("final_url_selection_ai", ctx)

    def test_llm_env_keys_no_longer_do_anything(self):
        ctx = _run(GMAPS_CONFIDENCE_MODE="llm", GMAPS_LLM_BATCH="true").context
        self.assertEqual(ctx["gmaps_confidence"]["mode"], "heuristic")
        self.assertNotIn("final_url_selection_ai", ctx)
        self.assertEqual(ctx["cost_breakdown"]["gemini_cost_usd"], 0.0)


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

    def test_raw_payload_is_not_duplicated_into_persisted_state(self):
        """state.json is rewritten IN FULL on every row update, so inlining each row's
        provider payload made it the dominant cost of a run (0.85MB at 100 rows, and
        quadratic from there). The payload already lives in serpwow_response/."""
        ctx = _run().context
        self.assertNotIn("raw_response", ctx["gmaps"])
        # The useful fields survive.
        for key in ("query", "official_website", "credits", "request_count"):
            self.assertIn(key, ctx["gmaps"])

    def test_billed_empty_is_counted_separately_from_free_no_results(self):
        """HTTP 200 with zero results IS billed -> credits spent for no data, which is
        the scrape.do refund case. A 502 "no results" is free and must not count."""
        empty = dict(_gmaps_ctx(website=None))
        empty.update(billed_empty=True, credits=10, successful_requests=1,
                     failed_requests=0, raw_response={"results": []})
        self.assertEqual(_run(ctx=empty).context["cost_breakdown"]["scrapedo_billed_empty"], 1)

        free = dict(_gmaps_ctx(website=None))
        free.update(no_results=True, billed_empty=False, credits=0,
                    successful_requests=0, failed_requests=1,
                    raw_response={"results": []})
        cb = _run(ctx=free).context["cost_breakdown"]
        self.assertEqual(cb["scrapedo_billed_empty"], 0)
        self.assertEqual(cb["scrapedo_credits"], 0)

    def test_normal_row_reports_no_billed_empty(self):
        self.assertEqual(_run().context["cost_breakdown"]["scrapedo_billed_empty"], 0)

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
