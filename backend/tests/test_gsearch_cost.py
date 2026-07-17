import unittest
from unittest import mock

from app.services.serpwow import serpwow_reporting


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
            results = serpwow_reporting.state_to_entity_results(state)
            summary = serpwow_reporting.build_summary(state, results)
        cost = summary["cost"]
        self.assertEqual(cost["serpwow_searches"], 10)
        self.assertAlmostEqual(cost["serpwow_usd"], 0.0035, places=6)
        self.assertAlmostEqual(cost["total_usd"], 0.002 + 0.0035, places=6)

    def test_rate_unset_means_zero_serpwow_usd(self):
        state = _state_with(searches=10, gemini_cost=0.002)
        with mock.patch.dict("os.environ", {"SERPWOW_USD_PER_SEARCH": ""}, clear=False):
            results = serpwow_reporting.state_to_entity_results(state)
            summary = serpwow_reporting.build_summary(state, results)
        self.assertEqual(summary["cost"]["serpwow_usd"], 0.0)
        self.assertAlmostEqual(summary["cost"]["total_usd"], 0.002, places=6)

    def test_failed_attempts_are_visible_but_not_billed(self):
        state = _state_with(searches=10, billable=2, gemini_cost=0.0)
        with mock.patch.dict("os.environ", {"SERPWOW_USD_PER_SEARCH": "0.00035"}):
            summary = serpwow_reporting.build_summary(
                state, serpwow_reporting.state_to_entity_results(state))
        self.assertEqual(summary["cost"]["serpwow_searches"], 10)
        self.assertEqual(summary["cost"]["serpwow_billable_searches"], 2)
        self.assertAlmostEqual(summary["cost"]["serpwow_usd"], 0.0007)

    def test_legacy_state_infers_billable_count_from_formatted_results(self):
        state = _state_with(searches=3, billable=None, gemini_cost=0.0)
        state["rows"][0]["result"]["context"]["formatted_results"] = [
            {"success": False}, {"success": True}, {"success": False}]
        with mock.patch.dict("os.environ", {"SERPWOW_USD_PER_SEARCH": "0.00035"}):
            summary = serpwow_reporting.build_summary(
                state, serpwow_reporting.state_to_entity_results(state))
        self.assertEqual(summary["cost"]["serpwow_searches"], 3)
        self.assertEqual(summary["cost"]["serpwow_billable_searches"], 1)
        self.assertAlmostEqual(summary["cost"]["serpwow_usd"], 0.00035)


if __name__ == "__main__":
    unittest.main()
