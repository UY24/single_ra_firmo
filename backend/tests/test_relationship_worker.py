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


def _llm(status, url, score=90):
    return ({"relationship_status": status, "relationship_summary": "summary text",
             "official_website": url, "confidence_score": score,
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
        self.assertFalse(ctx["skip_llm"])
        # 3 phases fired (X + input_url present); phase3 was removed.
        self.assertEqual(search.await_count, 3)
        self.assertEqual(ctx["cost_breakdown"]["serpwow_request_count"], 3)

    def test_not_confirmed_gates_url_to_none(self):
        search = AsyncMock(return_value=_serp(["https://modal.com/"], "No relation."))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=_llm("not_confirmed", "https://modal.com/")), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(y_name="Modal", x_name="other",
                                input_url="", city="", country="")
        self.assertIsNone(resp.official_website)
        self.assertEqual(resp.context["row_error"], REL_ERROR_NOT_CONFIRMED)
        flags = resp.context["relationship"]["flags"]
        self.assertTrue(any(f["flag"] == "url_found_no_relationship" for f in flags))

    def test_x_domain_candidates_are_filtered_before_llm(self):
        search = AsyncMock(return_value=_serp(
            ["https://www.eastlinkcap.com/team", "https://modal.com/"], "text"))
        captured = {}

        def fake_llm(x, y, city, country, candidates, *a, **k):
            captured["candidates"] = candidates
            return _llm("confirmed", "https://modal.com/")

        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website", side_effect=fake_llm), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(y_name="Modal", x_name="eastlinkcap",
                                input_url="https://www.eastlinkcap.com/p", city="", country="")
        self.assertNotIn("https://www.eastlinkcap.com/team", captured["candidates"])
        self.assertIn("https://modal.com/", captured["candidates"])

    def test_zero_evidence_short_circuits_without_llm(self):
        search = AsyncMock(return_value=_serp([], ""))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website") as llm, \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(y_name="YUZU NOISE", x_name="m25vc",
                                input_url="", city="", country="")
        llm.assert_not_called()
        self.assertIsNone(resp.official_website)
        self.assertTrue(resp.context["skip_llm"])
        self.assertEqual(resp.context["row_error"], REL_ERROR_NO_EVIDENCE)
        self.assertEqual(resp.context["relationship"]["status"], "not_confirmed")

    def test_blank_x_short_circuits_without_llm(self):
        search = AsyncMock(return_value=_serp(["https://modal.com/"], "text"))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website") as llm, \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(y_name="Modal", x_name="",
                                input_url="", city="", country="")
        llm.assert_not_called()
        self.assertTrue(resp.context["skip_llm"])
        self.assertEqual(resp.context["row_error"], REL_ERROR_NO_X)
        # no phases fire: phase1/phase2 need X, phase4 needs a domain, and phase3 is
        # gone. The row short-circuits anyway, so no SerpWow search is wasted.
        self.assertEqual(search.await_count, 0)

    def test_batch_mode_defers_llm_but_gathers_evidence(self):
        search = AsyncMock(return_value=_serp(["https://modal.com/"], "evidence"))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website") as llm, \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "true"}):
            resp, _ = self._run(y_name="Modal", x_name="eastlinkcap",
                                input_url="", city="", country="")
        llm.assert_not_called()
        self.assertIsNone(resp.official_website)
        self.assertFalse(resp.context["skip_llm"])
        self.assertEqual(resp.context["ai_overview_texts"], ["evidence"])
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
                                input_url="", city="", country="")
        self.assertEqual(resp.official_website, "https://modal.com/")
        errored = [a for a in resp.context["search_attempts"] if a.get("error")]
        self.assertEqual(len(errored), 1)


if __name__ == "__main__":
    unittest.main()
