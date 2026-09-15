import unittest
from unittest import mock

from app.services.serpwow import reporting


def _state_with(searches: int, gemini_cost: float, billable: int | None = None) -> dict:
    cost = {"serpwow_request_count": searches}
    if billable is not None:
        cost["serpwow_billable_request_count"] = billable
    return {
        "upload_id": "u1", "company_name": "Co", "pipeline": "gsearch", "status": "completed",
        "rows": [{
            "company_name": "Co", "country": "US", "status": "completed",
            "result": {
                "official_website": "https://co.com",
                "gemini_cost_usd": gemini_cost,
                "context": {"cost_breakdown": cost},
            },
        }],
    }


class TestGsearchCost(unittest.TestCase):
    def test_serpwow_usd_computed_and_added(self):
        state = _state_with(searches=10, gemini_cost=0.002)
        with mock.patch.dict("os.environ", {"SERPWOW_USD_PER_SEARCH": "0.00035"}, clear=False):
            results = reporting.state_to_entity_results(state)
            summary = reporting.build_summary(state, results)
        cost = summary["cost"]
        self.assertEqual(cost["serpwow_searches"], 10)
        self.assertAlmostEqual(cost["serpwow_usd"], 0.0035, places=6)
        self.assertAlmostEqual(cost["total_usd"], 0.002 + 0.0035, places=6)

    def test_rate_unset_means_zero_serpwow_usd(self):
        state = _state_with(searches=10, gemini_cost=0.002)
        with mock.patch.dict("os.environ", {"SERPWOW_USD_PER_SEARCH": ""}, clear=False):
            results = reporting.state_to_entity_results(state)
            summary = reporting.build_summary(state, results)
        self.assertEqual(summary["cost"]["serpwow_usd"], 0.0)
        self.assertAlmostEqual(summary["cost"]["total_usd"], 0.002, places=6)

    def test_failed_attempts_are_visible_but_not_billed(self):
        state = _state_with(searches=10, billable=2, gemini_cost=0.0)
        with mock.patch.dict("os.environ", {"SERPWOW_USD_PER_SEARCH": "0.00035"}):
            summary = reporting.build_summary(
                state, reporting.state_to_entity_results(state))
        self.assertEqual(summary["cost"]["serpwow_searches"], 10)
        self.assertEqual(summary["cost"]["serpwow_billable_searches"], 2)
        self.assertAlmostEqual(summary["cost"]["serpwow_usd"], 0.0007)

    def test_legacy_state_infers_billable_count_from_formatted_results(self):
        state = _state_with(searches=3, billable=None, gemini_cost=0.0)
        state["rows"][0]["result"]["context"]["formatted_results"] = [
            {"success": False}, {"success": True}, {"success": False}]
        with mock.patch.dict("os.environ", {"SERPWOW_USD_PER_SEARCH": "0.00035"}):
            summary = reporting.build_summary(
                state, reporting.state_to_entity_results(state))
        self.assertEqual(summary["cost"]["serpwow_searches"], 3)
        self.assertEqual(summary["cost"]["serpwow_billable_searches"], 1)
        self.assertAlmostEqual(summary["cost"]["serpwow_usd"], 0.00035)


def _gmaps_state(rows: list[dict]) -> dict:
    return {
        "upload_id": "u1", "company_name": "Co", "pipeline": "gmaps", "status": "completed",
        "rows": [{
            "company_name": "Co", "country": "US", "status": "completed",
            "result": {"official_website": "https://co.com", "gemini_cost_usd": 0.0,
                       "context": {"cost_breakdown": cb}},
        } for cb in rows],
    }


class TestScrapedoCredits(unittest.TestCase):
    """gmaps bills scrape.do credits (10 per successful call), not per-search USD."""

    # A real gmaps row: scrapedo_* only, no serpwow_* keys whatsoever.
    SCRAPEDO_ROW = {"scrapedo_requests": 1, "scrapedo_successful_requests": 1,
                    "scrapedo_failed_requests": 0, "scrapedo_credits": 10}

    def test_no_results_is_reported_apart_from_real_errors(self):
        """Reproduces the observed 100-row run: 88 billed + 12 "no listing" 502s.
        Those 12 must NOT read as failures — they're unbilled, unretried, and expected."""
        ok = dict(self.SCRAPEDO_ROW)
        no_listing = {"scrapedo_requests": 1, "scrapedo_successful_requests": 0,
                      "scrapedo_failed_requests": 1, "scrapedo_credits": 0,
                      "scrapedo_no_results": 1, "scrapedo_error_requests": 0}
        state = _gmaps_state([ok] * 88 + [no_listing] * 12)
        summary = reporting.build_summary(
            state, reporting.state_to_entity_results(state))
        cost = summary["cost"]
        self.assertEqual(cost["scrapedo_requests"], 100)       # not 200
        self.assertEqual(cost["scrapedo_successful_requests"], 88)
        self.assertEqual(cost["scrapedo_no_results"], 12)
        self.assertEqual(cost["scrapedo_credits"], 880)        # 10 x 88 only
        # Real errors = failed minus no-results => zero on a clean run.
        self.assertEqual(cost["scrapedo_failed_requests"] - cost["scrapedo_no_results"], 0)
        line = reporting._cost_log_line(summary)
        self.assertIn("ok=88", line)
        self.assertIn("no_listing=12", line)
        self.assertIn("errors=0", line)
        self.assertIn("rows_no_listing=12", line)

    def test_a_row_that_recovered_after_502_is_not_an_error(self):
        """User requirement: a 502 that succeeded on retry must NOT show as an error.
        Only a row that failed after every retry counts."""
        recovered = {"scrapedo_requests": 3, "scrapedo_successful_requests": 1,
                     "scrapedo_failed_requests": 2, "scrapedo_credits": 10,
                     "scrapedo_recovered_requests": 2, "scrapedo_error_requests": 0}
        dead = {"scrapedo_requests": 4, "scrapedo_successful_requests": 0,
                "scrapedo_failed_requests": 4, "scrapedo_credits": 0,
                "scrapedo_recovered_requests": 0, "scrapedo_error_requests": 4}
        state = _gmaps_state([recovered] * 5 + [dead] * 2)
        cost = reporting.build_summary(
            state, reporting.state_to_entity_results(state))["cost"]
        self.assertEqual(cost["scrapedo_recovered_requests"], 10)  # 5 rows x 2 retries
        self.assertEqual(cost["scrapedo_error_requests"], 8)       # only the 2 dead rows
        self.assertEqual(cost["scrapedo_credits"], 50)             # 5 rows billed once each
        line = reporting._cost_log_line({"cost": cost})
        self.assertIn("recovered=10", line)
        self.assertIn("errors=8", line)

    def test_billed_empty_rows_are_summed_for_the_refund_claim(self):
        ok = dict(self.SCRAPEDO_ROW)
        empty = {"scrapedo_requests": 1, "scrapedo_successful_requests": 1,
                 "scrapedo_failed_requests": 0, "scrapedo_credits": 10,
                 "scrapedo_billed_empty": 1}
        state = _gmaps_state([ok] * 90 + [empty] * 10)
        summary = reporting.build_summary(
            state, reporting.state_to_entity_results(state))
        cost = summary["cost"]
        self.assertEqual(cost["scrapedo_billed_empty"], 10)
        # 100 billed calls, 10 of which bought nothing => 100 credits wasted.
        self.assertEqual(cost["scrapedo_credits"], 1000)
        self.assertEqual(cost["scrapedo_billed_empty"] * 10, 100)

    def test_run_reconciles_calls_into_succeeded_plus_failed(self):
        # Mirrors the observed 100-row run: 88 rows succeeded first try, 12 rows burned
        # 4 attempts each and failed. 88 billed calls = 880 credits; 48 failed = free.
        ok = dict(self.SCRAPEDO_ROW)
        bad = {"scrapedo_requests": 4, "scrapedo_successful_requests": 0,
               "scrapedo_failed_requests": 4, "scrapedo_credits": 0}
        state = _gmaps_state([ok] * 88 + [bad] * 12)
        summary = reporting.build_summary(
            state, reporting.state_to_entity_results(state))
        cost = summary["cost"]
        self.assertEqual(cost["scrapedo_successful_requests"], 88)
        self.assertEqual(cost["scrapedo_failed_requests"], 48)
        self.assertEqual(cost["scrapedo_requests"], 136)
        self.assertEqual(cost["scrapedo_requests"],
                         cost["scrapedo_successful_requests"] + cost["scrapedo_failed_requests"])
        self.assertEqual(cost["scrapedo_credits"], 880)
        self.assertEqual(cost["scrapedo_credits"], 10 * cost["scrapedo_successful_requests"])

    def test_credits_are_summed_across_rows(self):
        state = _gmaps_state([self.SCRAPEDO_ROW] * 3)
        with mock.patch.dict("os.environ", {"SERPWOW_USD_PER_SEARCH": "0.00035"}):
            summary = reporting.build_summary(
                state, reporting.state_to_entity_results(state))
        cost = summary["cost"]
        self.assertEqual(cost["scrapedo_requests"], 3)
        self.assertEqual(cost["scrapedo_credits"], 30)
        # The SerpWow rate must not price a scrape.do run.
        self.assertEqual(cost["serpwow_searches"], 0)
        self.assertEqual(cost["serpwow_usd"], 0.0)
        self.assertEqual(cost["total_usd"], 0.0)

    def test_formatted_results_cannot_infer_billable_serpwow_searches(self):
        # Regression: gmaps rows carry a formatted_results entry for their single
        # scrape.do call. Without the scrapedo_* routing in build_summary, the
        # billable-count fallback would read success=True from it and bill the row at
        # SERPWOW_USD_PER_SEARCH.
        state = _gmaps_state([self.SCRAPEDO_ROW] * 2)
        for row in state["rows"]:
            row["result"]["context"]["formatted_results"] = [
                {"phase": "gmaps", "success": True, "error": None}]
        with mock.patch.dict("os.environ", {"SERPWOW_USD_PER_SEARCH": "0.00035"}):
            summary = reporting.build_summary(
                state, reporting.state_to_entity_results(state))
        self.assertEqual(summary["cost"]["serpwow_billable_searches"], 0)
        self.assertEqual(summary["cost"]["serpwow_usd"], 0.0)
        self.assertEqual(summary["cost"]["scrapedo_credits"], 20)

    def test_pre_migration_gmaps_run_still_reports_serpwow_searches(self):
        # Back-compat: a run recorded before the migration has no scrapedo_* keys.
        state = _gmaps_state([{"serpwow_request_count": 4}])
        with mock.patch.dict("os.environ", {"SERPWOW_USD_PER_SEARCH": "0.00035"}):
            summary = reporting.build_summary(
                state, reporting.state_to_entity_results(state))
        cost = summary["cost"]
        self.assertEqual(cost["serpwow_searches"], 4)
        self.assertAlmostEqual(cost["serpwow_usd"], 0.0014)
        self.assertEqual(cost["scrapedo_credits"], 0)

    def test_run_log_shows_credits_only_for_scrapedo_runs(self):
        scrapedo = reporting._cost_log_line(
            {"cost": {"llm_usd": 0.0, "serpwow_usd": 0.0, "total_usd": 0.0,
                      "serpwow_searches": 0, "scrapedo_requests": 3,
                      "scrapedo_credits": 30}})
        self.assertIn("scrapedo_requests=3", scrapedo)
        self.assertIn("scrapedo_credits=30", scrapedo)

        serpwow = reporting._cost_log_line(
            {"cost": {"llm_usd": 0.0, "serpwow_usd": 0.0035, "total_usd": 0.0035,
                      "serpwow_searches": 10, "scrapedo_requests": 0,
                      "scrapedo_credits": 0}})
        self.assertIn("serpwow_searches=10", serpwow)
        self.assertNotIn("scrapedo", serpwow)


if __name__ == "__main__":
    unittest.main()
