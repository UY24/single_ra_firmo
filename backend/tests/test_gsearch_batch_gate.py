import unittest
from unittest import mock

from app.services.serpwow import engine as legacy_app


class TestBatchGate(unittest.TestCase):
    def test_gsearch_uses_gsearch_flag(self):
        with mock.patch.dict("os.environ", {"GSEARCH_LLM_BATCH": "true"}):
            self.assertTrue(legacy_app._batch_postprocess_enabled_for("gsearch"))
            self.assertFalse(legacy_app._batch_postprocess_enabled_for("gmaps"))

    def test_other_pipeline_never_enabled(self):
        with mock.patch.dict("os.environ", {"GSEARCH_LLM_BATCH": "true"}):
            self.assertFalse(legacy_app._batch_postprocess_enabled_for("gmaps"))
