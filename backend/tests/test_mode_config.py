# backend/tests/test_mode_config.py
import os, unittest
from unittest import mock
from app.services.ai_mode.mode_config import MODES, get_mode

class TestModeConfig(unittest.TestCase):
    def test_modes_exist(self):
        self.assertEqual(set(MODES), {"ai_bulk", "ai_deep"})

    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_BULK_BATCH_SIZE", None)
            os.environ.pop("SCRAPEDO_BATCH_SIZE", None)
            os.environ.pop("AI_DEEP_BATCH_SIZE", None)
            self.assertEqual(get_mode("ai_bulk").batch_size(), 10)
            self.assertEqual(get_mode("ai_deep").batch_size(), 3)

    def test_bulk_falls_back_to_legacy_env(self):
        with mock.patch.dict(os.environ, {"SCRAPEDO_BATCH_SIZE": "7"}):
            os.environ.pop("AI_BULK_BATCH_SIZE", None)
            self.assertEqual(get_mode("ai_bulk").batch_size(), 7)

    def test_unknown_mode_raises(self):
        with self.assertRaises(KeyError):
            get_mode("ai_turbo")

    def test_malformed_env_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"AI_BULK_BATCH_SIZE": "ten"}):
            os.environ.pop("SCRAPEDO_BATCH_SIZE", None)
            self.assertEqual(get_mode("ai_bulk").batch_size(), 10)

    def test_malformed_primary_env_falls_through_to_legacy(self):
        with mock.patch.dict(os.environ, {"AI_BULK_BATCH_SIZE": "  ",
                                          "SCRAPEDO_BATCH_SIZE": "7"}):
            self.assertEqual(get_mode("ai_bulk").batch_size(), 7)
        with mock.patch.dict(os.environ, {"AI_BULK_BATCH_SIZE": "ten",
                                          "SCRAPEDO_BATCH_SIZE": "garbage"}):
            self.assertEqual(get_mode("ai_bulk").batch_size(), 10)

if __name__ == "__main__":
    unittest.main()
