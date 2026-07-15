import unittest
from unittest import mock

from app.services.serpwow import engine as legacy_app


class TestGmapsBatchEnabled(unittest.TestCase):
    def test_llm_and_batch_true_enables(self):
        with mock.patch.dict("os.environ", {"GMAPS_CONFIDENCE_MODE": "llm", "GMAPS_LLM_BATCH": "true"}):
            self.assertTrue(legacy_app._batch_postprocess_enabled_for("gmaps"))

    def test_llm_without_batch_disabled(self):
        with mock.patch.dict("os.environ", {"GMAPS_CONFIDENCE_MODE": "llm", "GMAPS_LLM_BATCH": "false"}):
            self.assertFalse(legacy_app._batch_postprocess_enabled_for("gmaps"))

    def test_heuristic_with_batch_flag_disabled(self):
        with mock.patch.dict("os.environ", {"GMAPS_CONFIDENCE_MODE": "heuristic", "GMAPS_LLM_BATCH": "true"}):
            self.assertFalse(legacy_app._batch_postprocess_enabled_for("gmaps"))

    def test_gsearch_gate_unchanged(self):
        with mock.patch.dict("os.environ", {"GSEARCH_LLM_BATCH": "true"}):
            self.assertTrue(legacy_app._batch_postprocess_enabled_for("gsearch"))
        with mock.patch.dict("os.environ", {"GSEARCH_LLM_BATCH": "false"}):
            self.assertFalse(legacy_app._batch_postprocess_enabled_for("gsearch"))


class TestGmapsBatchPending(unittest.TestCase):
    def _state(self, status):
        return {"pipeline": "gmaps", "gemini_batch": {"status": status}}

    def test_pending_true_while_running(self):
        with mock.patch.dict("os.environ", {"GMAPS_CONFIDENCE_MODE": "llm", "GMAPS_LLM_BATCH": "true"}):
            self.assertTrue(legacy_app._batch_postprocess_pending(self._state("running")))
            self.assertTrue(legacy_app._batch_postprocess_pending(self._state("queued")))

    def test_pending_false_when_terminal(self):
        with mock.patch.dict("os.environ", {"GMAPS_CONFIDENCE_MODE": "llm", "GMAPS_LLM_BATCH": "true"}):
            self.assertFalse(legacy_app._batch_postprocess_pending(self._state("succeeded")))

    def test_pending_false_for_heuristic_gmaps(self):
        with mock.patch.dict("os.environ", {"GMAPS_CONFIDENCE_MODE": "heuristic"}):
            self.assertFalse(legacy_app._batch_postprocess_pending(self._state("running")))
