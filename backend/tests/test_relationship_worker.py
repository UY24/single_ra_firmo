# backend/tests/test_relationship_worker.py
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.services.serpwow.constants import (
    REL_ERROR_NO_EVIDENCE,
    REL_ERROR_NO_X,
    REL_ERROR_NOT_CONFIRMED,
)
from app.services.serpwow.modes.relationship import execute_relationship_lookup_for_worker

MODPATH = "app.services.serpwow.modes.relationship"


def _serp(candidates, overview_text="", official=None):
    raw = {}
    if overview_text:
        raw["ai_overview"] = {
            "ai_overview_contents": [{"text": overview_text}],
            "ai_overview_sources": [],
        }
    return {"provider": "serpwow", "used": True, "query": "q",
            "official_website": official, "candidates": list(candidates),
            "status_code": 200, "search_url": "https://google/x",
            "raw_response": raw, "error": None}


def _failed_serp(status=503):
    return {"provider": "serpwow", "used": False, "query": "q",
            "official_website": None, "candidates": [],
            "status_code": status, "search_url": None,
            "raw_response": None,
            "error": f"SerpWow failed (HTTP {status}).",
            "error_category": "http_5xx"}


def _llm(status, url, score=90):
    return ({"resolved_company_y_name": "Modal Labs",
             "relationship_status": status, "relationship_summary": "summary text",
             "relationship_evidence": ["Eastlink invested in Modal."],
             "official_website": url,
             "relationship_confidence_score": score,
             "website_confidence_score": score if url else 0,
             "reason": "r", "extra_flags": []},
            None, "gemini-2.5-flash-lite",
            {"promptTokenCount": 10, "candidatesTokenCount": 5})


class TestRelationshipExecutor(unittest.TestCase):
    def _run(self, **kwargs):
        return asyncio.run(execute_relationship_lookup_for_worker(**kwargs))

    def test_confirmed_relationship_yields_url(self):
        search = AsyncMock(return_value=_serp(
            ["https://modal.com/"], "Eastlink invests in Modal Labs."))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=_llm("confirmed", "https://modal.com/")), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, raw_json = self._run(
                y_name="Modal", x_name="eastlinkcap",
                input_url="https://www.eastlinkcap.com/portfolio/", city="", country="")
        self.assertEqual(resp.official_website, "https://modal.com/")
        ctx = resp.context
        self.assertEqual(ctx["pipeline"], "relationship")
        self.assertEqual(ctx["relationship"]["status"], "confirmed")
        self.assertEqual(ctx["relationship"]["verified_pair"], "eastlinkcap ↔ Modal")
        self.assertEqual(ctx["relationship"]["resolved_company_y_name"], "Modal Labs")
        self.assertEqual(ctx["relationship"]["evidence"],
                         ["Eastlink invested in Modal."])
        self.assertEqual(ctx["relationship"]["relationship_confidence_score"], 90)
        self.assertEqual(ctx["relationship"]["website_confidence_score"], 90)
        self.assertEqual(ctx["final_url_selection_ai"]["raw"]["confidence_score"], 90)
        self.assertFalse(ctx["skip_llm"])
        # Exactly three phases fire for every valid pair.
        self.assertEqual(search.await_count, 3)
        self.assertEqual(ctx["cost_breakdown"]["serpwow_request_count"], 3)

    def test_not_confirmed_gates_url_to_none(self):
        search = AsyncMock(return_value=_serp(["https://modal.com/"], "No relation."))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=_llm("not_confirmed", "https://modal.com/")), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(y_name="Modal", x_name="other",
                                input_url="https://other.example/portfolio",
                                city="", country="")
        self.assertIsNone(resp.official_website)
        self.assertEqual(resp.context["row_error"], REL_ERROR_NOT_CONFIRMED)
        flags = resp.context["relationship"]["flags"]
        self.assertTrue(any(f["flag"] == "url_found_no_relationship" for f in flags))

    def test_x_domain_candidates_are_filtered_before_llm(self):
        search = AsyncMock(return_value=_serp(
            ["https://www.eastlinkcap.com/team", "https://modal.com/"], "text"))
        captured = {}

        def fake_llm(x, y, input_url, city, country, candidates, *a, **k):
            captured["candidates"] = candidates
            return _llm("confirmed", "https://modal.com/")

        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website", side_effect=fake_llm), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(y_name="Modal", x_name="eastlinkcap",
                                input_url="https://www.eastlinkcap.com/p", city="", country="")
        self.assertNotIn("https://www.eastlinkcap.com/team", captured["candidates"])
        self.assertIn("https://modal.com/", captured["candidates"])

    def test_typed_overview_url_becomes_candidate_and_evidence_keeps_phase(self):
        raw = {
            "ai_overview": {
                "ai_overview_contents": [
                    {"type": "paragraph", "text": "Eastlink invested in Modal."},
                    {"type": "list", "list": [
                        {"text": "Modal website: https://modal.com/"},
                        {"text": "Investor: https://eastlinkcap.com/portfolio"},
                    ]},
                ],
                "ai_overview_sources": [{
                    "source_name": "Funding report",
                    "source_url": "https://news.example/modal",
                }],
            }
        }
        search_result = _serp([], "")
        search_result["raw_response"] = raw
        captured = {}

        def fake_llm(x, y, input_url, city, country, candidates, evidence,
                     *args, **kwargs):
            captured["candidates"] = candidates
            captured["evidence"] = evidence
            return _llm("confirmed", "https://modal.com/")

        with patch(f"{MODPATH}.run_serpwow_search",
                   AsyncMock(return_value=search_result)) as search, \
             patch(f"{MODPATH}.choose_relationship_and_website", side_effect=fake_llm), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(
                y_name="Modal", x_name="eastlinkcap",
                input_url="https://eastlinkcap.com/portfolio", city="", country="")

        self.assertEqual(search.await_count, 3)
        self.assertIn("https://modal.com/", captured["candidates"])
        self.assertNotIn("https://eastlinkcap.com/portfolio", captured["candidates"])
        self.assertEqual(len(captured["evidence"]), 3)
        self.assertEqual(captured["evidence"][0]["phase"],
                         "phase1_official_website")
        self.assertIn("2. Investor:", captured["evidence"][0]["text"])
        self.assertEqual(captured["evidence"][0]["sources"], [{
            "name": "Funding report", "url": "https://news.example/modal"}])
        self.assertEqual(resp.context["ai_overview_evidence"], captured["evidence"])
        attempts = resp.context["search_attempts"]
        self.assertEqual(len(attempts), 3)
        self.assertEqual(attempts[0]["result"],
                         "AI overview returned; 1 candidate(s)")
        self.assertTrue(attempts[0]["ai_overview_present"])
        self.assertEqual(attempts[0]["candidate_count"], 1)
        self.assertEqual(attempts[0]["status"], "candidates_found")

    def test_zero_evidence_short_circuits_without_llm(self):
        search = AsyncMock(return_value=_serp([], ""))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website") as llm, \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(y_name="YUZU NOISE", x_name="m25vc",
                                input_url="https://m25vc.com/portfolio",
                                city="", country="")
        llm.assert_not_called()
        self.assertIsNone(resp.official_website)
        self.assertTrue(resp.context["skip_llm"])
        self.assertEqual(resp.context["row_error"], REL_ERROR_NO_EVIDENCE)
        self.assertEqual(resp.context["relationship"]["status"], "not_confirmed")

    def test_failed_serpwow_attempts_are_not_billed(self):
        search = AsyncMock(return_value=_failed_serp(503))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website") as llm, \
             patch.dict("os.environ", {
                 "RELATIONSHIP_LLM_BATCH": "false",
                 "SERPWOW_USD_PER_SEARCH": "0.00035",
             }):
            resp, _ = self._run(y_name="Modal", x_name="eastlinkcap",
                                input_url="https://eastlinkcap.com/portfolio",
                                city="", country="")
        llm.assert_not_called()
        self.assertEqual(resp.serpwow_cost_usd, 0.0)
        self.assertEqual(resp.context["cost_breakdown"]["serpwow_request_count"], 3)
        self.assertEqual(
            resp.context["cost_breakdown"]["serpwow_billable_request_count"], 0)
        self.assertEqual(resp.context["relationship"]["summary"],
                         "SerpWow failed (HTTP 503).")
        self.assertTrue(any(
            flag["flag"] == "serpwow_failed"
            for flag in resp.context["relationship"]["flags"]))

    def test_blank_x_short_circuits_without_llm(self):
        search = AsyncMock(return_value=_serp(["https://modal.com/"], "text"))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website") as llm, \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false",
                                        "SERPWOW_USD_PER_SEARCH": "0.00035"}):
            resp, _ = self._run(y_name="Modal", x_name="",
                                input_url="", city="", country="")
        llm.assert_not_called()
        self.assertTrue(resp.context["skip_llm"])
        self.assertEqual(resp.context["row_error"], REL_ERROR_NO_X)
        # Invalid direct calls cannot build any phase queries, so no search is wasted.
        self.assertEqual(search.await_count, 0)

    def test_batch_mode_defers_llm_but_gathers_evidence(self):
        search = AsyncMock(return_value=_serp(["https://modal.com/"], "evidence"))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website") as llm, \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "true"}):
            resp, _ = self._run(y_name="Modal", x_name="eastlinkcap",
                                input_url="https://eastlinkcap.com/portfolio",
                                city="", country="")
        llm.assert_not_called()
        self.assertIsNone(resp.official_website)
        self.assertFalse(resp.context["skip_llm"])
        self.assertEqual(
            [item["text"] for item in resp.context["ai_overview_evidence"]],
            ["evidence", "evidence", "evidence"],
        )
        self.assertIn("https://modal.com/", resp.context["candidates"])

    def test_phase_error_captured_not_fatal(self):
        ok = _serp(["https://modal.com/"], "text")

        async def flaky(query, country=None, client=None):
            # Fail only the phase2 investment-evidence query; phase1 still succeeds.
            if "investment OR portfolio" in query:
                raise RuntimeError("boom")
            return ok

        with patch(f"{MODPATH}.run_serpwow_search", side_effect=flaky), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=_llm("confirmed", "https://modal.com/")), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(y_name="Modal", x_name="eastlinkcap",
                                input_url="https://eastlinkcap.com/portfolio",
                                city="", country="")
        self.assertEqual(resp.official_website, "https://modal.com/")
        errored = [a for a in resp.context["search_attempts"] if a.get("error")]
        self.assertEqual(len(errored), 1)
        self.assertEqual(errored[0]["status"], "error")
        self.assertEqual(
            resp.context["cost_breakdown"]["serpwow_billable_request_count"], 2)
        self.assertAlmostEqual(resp.serpwow_cost_usd, 0.0007)


if __name__ == "__main__":
    unittest.main()
