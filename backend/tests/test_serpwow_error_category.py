# backend/tests/test_serpwow_error_category.py
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from app.services.serpwow import serpwow_client as sc
from app.services.serpwow import outcomes as o


class TestErrorCategory(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_categorized(self):
        client = AsyncMock()
        client.get.side_effect = httpx.ReadTimeout("timed out")
        with patch.dict("os.environ", {"SERPWOW_API_KEY": "k"}):
            out = await sc.run_serpwow_search("q", country="US", client=client)
        self.assertFalse(out["used"])
        self.assertEqual(out["error_category"], o.CAT_TIMEOUT)

    async def test_429_categorized(self):
        req = httpx.Request("GET", "https://api.serpwow.com/live/search")
        resp = httpx.Response(429, request=req)
        client = AsyncMock()
        client.get.return_value = resp
        with patch.dict("os.environ", {"SERPWOW_API_KEY": "k"}):
            out = await sc.run_serpwow_search("q", country="US", client=client)
        self.assertFalse(out["used"])
        self.assertEqual(out["error_category"], o.CAT_RATE_LIMIT)

    async def test_success_has_no_category(self):
        req = httpx.Request("GET", "https://api.serpwow.com/live/search")
        resp = httpx.Response(200, json={"request_info": {}, "organic_results": []}, request=req)
        client = AsyncMock()
        client.get.return_value = resp
        with patch.dict("os.environ", {"SERPWOW_API_KEY": "k"}):
            out = await sc.run_serpwow_search("q", country="US", client=client)
        self.assertTrue(out["used"])
        self.assertIsNone(out["error_category"])
