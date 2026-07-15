# backend/tests/test_relationship_worker.py
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.services.serpwow.constants import (
    REL_ERROR_NO_EVIDENCE,
    REL_ERROR_NO_X,
    REL_ERROR_NOT_CONFIRMED,
    REL_REASON_CONFIRMED_URL_NOT_VALIDATED,
    REL_REASON_MISSING_X,
    REL_REASON_NOT_CONFIRMED,
    REL_REASON_NO_EVIDENCE,
    REL_REASON_UNCLEAR,
)
from app.services.serpwow.modes.relationship import execute_relationship_lookup_for_worker
from app.services.serpwow.outcomes import (
    OUTCOME_ERROR,
    SRC_SERPWOW,
    classify_finalized_row,
)

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


def _llm(status, url, score=90,
         evidence_id="phase1_relationship_and_url.overview.0"):
    return ({"relationship_status": status, "relationship_summary": "summary text",
             "official_website": url, "confidence_score": score,
             "supporting_evidence_ids": (
                 [evidence_id]
                 if status == "confirmed" else []),
             "reason": "r", "extra_flags": []},
            None, "gemini-2.5-flash-lite",
            {"promptTokenCount": 10, "candidatesTokenCount": 5})


class TestRelationshipExecutor(unittest.TestCase):
    def _run(self, **kwargs):
        return asyncio.run(execute_relationship_lookup_for_worker(**kwargs))

    def test_confirmed_relationship_yields_url(self):
        search = AsyncMock(return_value=_serp(
            ["https://modal.com/"], "eastlinkcap invests in Modal."))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=_llm("confirmed", "https://modal.com/")), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, raw_json = self._run(
                y_name="Modal", x_name="eastlinkcap",
                input_url="https://www.eastlinkcap.com/portfolio/", city="", country="")
        self.assertEqual(resp.official_website, "https://modal.com")
        ctx = resp.context
        self.assertEqual(ctx["pipeline"], "relationship")
        self.assertEqual(ctx["relationship"]["status"], "confirmed")
        self.assertEqual(ctx["relationship"]["reason_code"], "")
        self.assertEqual(ctx["relationship"]["verified_pair"], "eastlinkcap ↔ Modal")
        self.assertFalse(ctx["skip_llm"])
        self.assertEqual(search.await_count, 1)
        self.assertEqual(ctx["cost_breakdown"]["serpwow_request_count"], 1)
        self.assertEqual(ctx["executed_phases"],
                         ["phase1_relationship_and_url"])
        self.assertEqual(ctx["candidates"], ["https://modal.com"])
        self.assertEqual(json.loads(raw_json)["queries"], [
            ["phase1_relationship_and_url", search.await_args.args[0]],
        ])

    def test_sync_passes_structured_evidence_and_stores_only_accepted_records(self):
        search = AsyncMock(return_value=_serp(
            ["https://modal.com/"], "eastlinkcap invests in Modal."))
        captured = {}

        def fake_llm(x, y, city, country, candidates, candidate_evidence,
                     evidence, search_attempts, phase4_hit, x_domain):
            captured["candidate_evidence"] = candidate_evidence
            captured["evidence"] = evidence
            return ({
                "relationship_status": "confirmed",
                "relationship_summary": "Grounded summary.",
                "official_website": "https://modal.com/",
                "confidence_score": 90,
                "supporting_evidence_ids": [
                    "phase1_relationship_and_url.overview.0",
                    "unknown-id",
                    "phase1_relationship_and_url.candidate.0",
                ],
                "extra_flags": [],
            }, None, "gemini-2.5-flash-lite", {})

        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website", side_effect=fake_llm), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(
                y_name="Modal", x_name="eastlinkcap",
                input_url="https://eastlinkcap.com/portfolio", city="", country="")

        candidate_record = captured["candidate_evidence"][0]
        relationship_record = captured["evidence"][0]
        self.assertEqual(resp.context["relationship"]["evidence"], [
            candidate_record, relationship_record,
        ])
        self.assertEqual(
            resp.context["final_url_selection_ai"]["raw"]["supporting_evidence_ids"],
            ["phase1_relationship_and_url.overview.0",
             "phase1_relationship_and_url.candidate.0"],
        )
        flags = resp.context["relationship"]["flags"]
        self.assertTrue(any(flag["flag"] == "unknown_evidence_id" for flag in flags))

    def test_grounding_downgrade_hides_unsupported_summary_and_model_flags(self):
        search = AsyncMock(return_value=_serp(
            ["https://modal.com/"], "Modal makes developer tools."))
        model_output = {
            "relationship_status": "confirmed",
            "relationship_summary": "Eastlink invested in Modal.",
            "official_website": "https://modal.com/",
            "confidence_score": 90,
            "supporting_evidence_ids": [],
            "extra_flags": ["invented_transaction"],
        }
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=(model_output, None, "m", {})), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(
                y_name="Modal", x_name="eastlinkcap",
                input_url="https://eastlinkcap.com", city="", country="")

        relationship = resp.context["relationship"]
        self.assertEqual(relationship["status"], "unclear")
        self.assertEqual(
            relationship["summary"],
            "Confirmation was rejected because no supplied relationship evidence was cited.",
        )
        self.assertFalse(any(
            flag["flag"] == "invented_transaction" for flag in relationship["flags"]
        ))
        self.assertEqual(
            resp.context["final_url_selection_ai"]["raw"]["relationship_summary"],
            "Eastlink invested in Modal.",
        )

    def test_sync_negative_overview_citation_cannot_confirm(self):
        search = AsyncMock(return_value=_serp(
            ["https://modal.com/"],
            "No investment is documented between eastlinkcap and Modal."))
        model_output = {
            "relationship_status": "confirmed",
            "relationship_summary": "Unsupported conclusion.",
            "official_website": "https://modal.com/",
            "confidence_score": 90,
            "supporting_evidence_ids": [
                "phase1_relationship_and_url.overview.0",
            ],
            "extra_flags": [],
        }
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=(model_output, None, "m", {})), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(
                y_name="Modal", x_name="eastlinkcap",
                input_url="eastlinkcap.com/portfolio", city="", country="")

        self.assertIsNone(resp.official_website)
        self.assertEqual(resp.context["relationship"]["status"], "unclear")
        self.assertEqual(resp.context["x_domain"], "eastlinkcap.com")

    def test_sync_noisy_ocr_can_confirm_from_affirmative_source_assertion(self):
        y_ocr = "YUZU SPARKLINGWE SANZO POMELO"
        search = AsyncMock(return_value=_serp(
            ["https://sanzo.com"],
            "Eastlink Capital invested in Sanzo."))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=_llm("confirmed", "https://sanzo.com")), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(
                y_name=y_ocr, x_name="Eastlink Capital",
                input_url="https://eastlinkcap.com/portfolio",
                city="", country="United States")

        self.assertEqual(resp.official_website, "https://sanzo.com")
        self.assertEqual(resp.context["relationship"]["status"], "confirmed")
        self.assertEqual(resp.context["executed_phases"], [
            "phase1_relationship_and_url",
            "phase2_financial_evidence",
        ])

    def test_ai_overview_texts_excludes_organic_snippets(self):
        response = _serp(
            ["https://modal.com/"], "eastlinkcap invested in Modal.")
        response["raw_response"]["organic_results"] = [{
            "link": "https://modal.com",
            "snippet": "Modal received funding from eastlinkcap.",
        }]
        search = AsyncMock(return_value=response)
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "true"}):
            resp, _ = self._run(
                y_name="Modal", x_name="eastlinkcap",
                input_url="eastlinkcap.com/portfolio", city="", country="")

        self.assertEqual(
            resp.context["ai_overview_texts"],
            ["eastlinkcap invested in Modal."],
        )
        self.assertEqual(len(resp.context["evidence"]), 2)

    def test_bare_x_domain_candidate_is_filtered_before_llm(self):
        search = AsyncMock(return_value=_serp(
            ["https://eastlinkcap.com/team", "https://modal.com/"],
            "eastlinkcap invested in Modal."))
        captured = {}

        def fake_llm(x, y, city, country, candidates, *args):
            captured["candidates"] = candidates
            return _llm("confirmed", "https://modal.com/")

        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website", side_effect=fake_llm), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            self._run(
                y_name="Modal", x_name="eastlinkcap",
                input_url="eastlinkcap.com/portfolio", city="", country="")

        self.assertEqual(captured["candidates"], ["https://modal.com"])

    def test_one_request_uses_configured_serpwow_cost_in_all_totals(self):
        search = AsyncMock(return_value=_serp(
            ["https://modal.com/"], "eastlinkcap invests in Modal."))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=_llm("confirmed", "https://modal.com/")), \
             patch.dict("os.environ", {
                 "RELATIONSHIP_LLM_BATCH": "false",
                 "SERPWOW_USD_PER_SEARCH": "0.00035",
             }, clear=False):
            resp, _ = self._run(
                y_name="Modal", x_name="eastlinkcap",
                input_url="https://eastlinkcap.com/portfolio", city="", country="")

        costs = resp.context["cost_breakdown"]
        self.assertEqual(search.await_count, 1)
        self.assertEqual(costs["serpwow_request_count"], 1)
        self.assertEqual(resp.serpwow_cost_usd, 0.00035)
        self.assertEqual(costs["serpwow_cost_usd"], 0.00035)
        self.assertEqual(resp.total_cost_usd,
                         resp.serpwow_cost_usd + resp.gemini_cost_usd)
        self.assertEqual(costs["total_cost_usd"], resp.total_cost_usd)

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
        self.assertEqual(
            resp.context["relationship"]["reason_code"],
            REL_REASON_NOT_CONFIRMED,
        )
        flags = resp.context["relationship"]["flags"]
        self.assertTrue(any(f["flag"] == "url_found_no_relationship" for f in flags))

    def test_confirmed_without_validated_url_keeps_grounded_relationship(self):
        search = AsyncMock(return_value=_serp(
            [], "Eastlink invested in Mødal, a developer platform."))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=_llm("confirmed", None, score=83)), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(
                y_name="Mødal", x_name="Eastlink",
                input_url="https://eastlinkcap.com", city="", country="")

        relationship = resp.context["relationship"]
        self.assertIsNone(resp.official_website)
        self.assertEqual(relationship["status"], "confirmed")
        self.assertEqual(relationship["summary"], "summary text")
        self.assertEqual(
            relationship["reason_code"],
            REL_REASON_CONFIRMED_URL_NOT_VALIDATED,
        )
        self.assertEqual(
            relationship["evidence"][0]["text"],
            "Eastlink invested in Mødal, a developer platform.",
        )
        self.assertTrue(any(
            flag["flag"] == REL_REASON_CONFIRMED_URL_NOT_VALIDATED
            for flag in relationship["flags"]
        ))
        self.assertEqual(
            resp.context["final_url_selection_ai"]["raw"]["confidence_score"], 83)

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
        self.assertIn("https://modal.com", captured["candidates"])

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
        self.assertEqual(
            resp.context["relationship"]["reason_code"], REL_REASON_NO_EVIDENCE)

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
        self.assertEqual(
            resp.context["relationship"]["reason_code"], REL_REASON_MISSING_X)
        self.assertEqual(search.await_count, 0)
        self.assertEqual(resp.context["cost_breakdown"]["serpwow_request_count"], 0)
        self.assertEqual(resp.serpwow_cost_usd, 0.0)

    def test_batch_mode_defers_llm_but_gathers_evidence(self):
        search = AsyncMock(return_value=_serp(
            ["https://modal.com/"], "eastlinkcap invested in Modal."))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website") as llm, \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "true"}):
            resp, _ = self._run(y_name="Modal", x_name="eastlinkcap",
                                input_url="", city="", country="")
        llm.assert_not_called()
        self.assertIsNone(resp.official_website)
        self.assertFalse(resp.context["skip_llm"])
        self.assertEqual(resp.context["ai_overview_texts"],
                         ["eastlinkcap invested in Modal."])
        self.assertIn("https://modal.com", resp.context["candidates"])
        self.assertEqual(resp.context["executed_phases"],
                         ["phase1_relationship_and_url"])

    def test_phase_error_captured_not_fatal(self):
        ok = _serp(
            ["https://modal.com/"], "eastlinkcap invested in Modal.")

        async def flaky(query, country=None, client=None):
            if "Identify the exact Company Y" in query:
                raise RuntimeError("boom")
            return ok

        with patch(f"{MODPATH}.run_serpwow_search", side_effect=flaky), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=_llm(
                       "confirmed", "https://modal.com/",
                       evidence_id="phase2_financial_evidence.overview.0")), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(y_name="Modal", x_name="eastlinkcap",
                                input_url="", city="", country="")
        self.assertEqual(resp.official_website, "https://modal.com")
        errored = [a for a in resp.context["search_attempts"] if a.get("error")]
        self.assertEqual(len(errored), 1)
        self.assertEqual(resp.context["executed_phases"], [
            "phase1_relationship_and_url",
            "phase2_financial_evidence",
        ])

    def test_relationship_evidence_without_url_jumps_to_url_recovery(self):
        search = AsyncMock(side_effect=[
            _serp([], "eastlinkcap invested in Modal."),
            _serp(["https://modal.com/"], ""),
        ])
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=_llm("confirmed", "https://modal.com")), \
             patch.dict("os.environ", {
                 "RELATIONSHIP_LLM_BATCH": "false",
                 "SERPWOW_USD_PER_SEARCH": "0.00035",
             }, clear=False):
            resp, _ = self._run(
                y_name="Modal", x_name="eastlinkcap",
                input_url="https://eastlinkcap.com/portfolio", city="", country="")

        self.assertEqual(search.await_count, 2)
        self.assertEqual(resp.context["executed_phases"], [
            "phase1_relationship_and_url",
            "phase3_official_url_recovery",
        ])
        self.assertEqual(resp.context["cost_breakdown"]["serpwow_request_count"], 2)
        self.assertEqual(resp.serpwow_cost_usd, 0.0007)

    def test_missing_relationship_runs_evidence_phase_before_url_recovery(self):
        search = AsyncMock(side_effect=[
            _serp([], "Modal makes developer tools."),
            _serp([], "eastlinkcap funded Modal."),
            _serp(["https://modal.com"], ""),
        ])
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=_llm("confirmed", "https://modal.com")), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(
                y_name="Modal", x_name="eastlinkcap",
                input_url="https://eastlinkcap.com", city="", country="")

        self.assertEqual(resp.context["executed_phases"], [
            "phase1_relationship_and_url",
            "phase2_financial_evidence",
            "phase3_official_url_recovery",
        ])

    def test_all_phase_errors_remain_technical_and_skip_llm(self):
        search = AsyncMock(side_effect=TimeoutError("provider timed out"))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website") as llm, \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(
                y_name="Modal", x_name="eastlinkcap",
                input_url="https://eastlinkcap.com", city="", country="")

        llm.assert_not_called()
        self.assertTrue(resp.context["skip_llm"])
        self.assertIsNone(resp.context["row_error"])
        self.assertEqual(resp.context["relationship"]["status"], "unclear")
        self.assertEqual(resp.context["relationship"]["reason_code"], "")
        self.assertTrue(any(
            flag["flag"] == "all_search_phases_failed"
            for flag in resp.context["relationship"]["flags"]
        ))
        self.assertEqual(search.await_count, 2)
        self.assertEqual(resp.context["cost_breakdown"]["serpwow_request_count"], 2)
        outcome = classify_finalized_row(
            {"official_website": resp.official_website, "context": resp.context},
            pipeline="relationship",
            ctx_row_error=resp.context["row_error"],
            skip_llm=resp.context["skip_llm"],
        )
        self.assertEqual((outcome.outcome, outcome.error_source),
                         (OUTCOME_ERROR, SRC_SERPWOW))

    def test_unclear_without_url_has_unclear_reason(self):
        search = AsyncMock(return_value=_serp(
            [], "The search results mention Eastlink and Modal ambiguously."))
        with patch(f"{MODPATH}.run_serpwow_search", search), \
             patch(f"{MODPATH}.choose_relationship_and_website",
                   return_value=_llm("unclear", None, score=42)), \
             patch.dict("os.environ", {"RELATIONSHIP_LLM_BATCH": "false"}):
            resp, _ = self._run(
                y_name="Modal", x_name="Eastlink",
                input_url="https://eastlinkcap.com", city="", country="")

        self.assertIsNone(resp.official_website)
        self.assertEqual(
            resp.context["relationship"]["reason_code"], REL_REASON_UNCLEAR)


if __name__ == "__main__":
    unittest.main()
