import unittest
from unittest import mock

from app.services.serpwow import legacy_app


class TestBatchGate(unittest.TestCase):
    def test_full_uses_enable_flag(self):
        with mock.patch.dict("os.environ", {"ENABLE_GEMINI_BATCH_POSTPROCESS": "true",
                                            "GSEARCH_LLM_BATCH": "false"}):
            self.assertTrue(legacy_app._batch_postprocess_enabled_for("full"))
            self.assertFalse(legacy_app._batch_postprocess_enabled_for("gsearch"))

    def test_gsearch_uses_gsearch_flag(self):
        with mock.patch.dict("os.environ", {"ENABLE_GEMINI_BATCH_POSTPROCESS": "false",
                                            "GSEARCH_LLM_BATCH": "true"}):
            self.assertTrue(legacy_app._batch_postprocess_enabled_for("gsearch"))
            self.assertFalse(legacy_app._batch_postprocess_enabled_for("full"))

    def test_other_pipeline_never_enabled(self):
        with mock.patch.dict("os.environ", {"ENABLE_GEMINI_BATCH_POSTPROCESS": "true",
                                            "GSEARCH_LLM_BATCH": "true"}):
            self.assertFalse(legacy_app._batch_postprocess_enabled_for("gmaps"))
