import os
import unittest
from unittest import mock

from app.services.serpwow.engine import calculate_serpwow_cost_usd


class TestSerpWowCostFn(unittest.TestCase):
    def test_with_serpwow_usd_per_search_set(self):
        """Test that SERPWOW_USD_PER_SEARCH takes precedence."""
        with mock.patch.dict(os.environ, {"SERPWOW_USD_PER_SEARCH": "0.001"}, clear=False):
            result = calculate_serpwow_cost_usd(5)
            expected = round(5 * 0.001, 8)
            self.assertEqual(result, expected)

    def test_with_legacy_var_fallback(self):
        """Test that SERPWOW_USD_PER_REQUEST is used when SEARCH var is not set."""
        with mock.patch.dict(
            os.environ,
            {"SERPWOW_USD_PER_REQUEST": "0.002"},
            clear=False
        ):
            # Ensure SEARCH var is not set
            os.environ.pop("SERPWOW_USD_PER_SEARCH", None)
            result = calculate_serpwow_cost_usd(5)
            expected = round(5 * 0.002, 8)
            self.assertEqual(result, expected)

    def test_zero_requests(self):
        """Test that zero requests returns 0.0."""
        with mock.patch.dict(os.environ, {"SERPWOW_USD_PER_SEARCH": "0.001"}, clear=False):
            result = calculate_serpwow_cost_usd(0)
            self.assertEqual(result, 0.0)


if __name__ == "__main__":
    unittest.main()
