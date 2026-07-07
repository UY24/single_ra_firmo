import asyncio
import unittest
from unittest import mock

from app.services.serpwow import engine as legacy_app
# execute_gmaps_lookup now lives in modes.gmaps and resolves run_gmaps_from_module /
# choose_final_website_with_gemini in that module's namespace, so mocks patch there.
from app.services.serpwow.modes import gmaps as gmaps_mode


def _gmaps_ctx(website="https://acme-motors.com"):
    return {
        "provider": "gmaps", "used": True, "query": "Acme Motors",
        "official_website": website, "request_count": 1, "error": None,
        "raw_response": {"results": [
            {"title": "Acme Motors", "website": website, "address": "500 Main Street"}]},
    }


def _run(**env):
    """Run execute_gmaps_lookup with run_gmaps_from_module mocked; env overrides applied."""
    with mock.patch.object(gmaps_mode, "run_gmaps_from_module",
                           new=mock.AsyncMock(return_value=_gmaps_ctx())), \
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
