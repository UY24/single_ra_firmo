# Regression tests for the engine.py decomposition: run_serpwow_search must be
# executable end-to-end (HTTP mocked) without engine.startup_event() having run.
# The 2026-07-07 split left `search_fetch_semaphore` defined only in engine.py,
# so every LIVE search died with a swallowed NameError while the mocked suite
# stayed green. These tests exercise the real function body.
import asyncio
import unittest
from unittest.mock import patch

from app.services.serpwow import engine, serpwow_client

class _FakeResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {
            "request_info": {"success": True},
            "search_metadata": {"engine_url": "https://google.com/search?q=x"},
            "organic_results": [
                {"title": "Acme", "link": "https://www.acme-widgets.example/"}
            ],
        }


class _FakeClient:
    async def get(self, url, params=None):
        return _FakeResponse()


class TestRunSerpwowSearchLivePath(unittest.TestCase):
    def test_executes_without_engine_startup(self) -> None:
        # No engine.startup_event(): semaphore must default to None, not NameError.
        self.assertIsNone(serpwow_client.search_fetch_semaphore)
        with patch.dict("os.environ", {"SERPWOW_API_KEY": "test-key"}):
            result = asyncio.run(
                serpwow_client.run_serpwow_search("acme widgets", client=_FakeClient())
            )
        self.assertIsNone(result.get("error"))
        self.assertTrue(result.get("used"))
        self.assertEqual(
            result.get("candidates"), ["https://www.acme-widgets.example/"]
        )

    def test_startup_event_publishes_semaphore_to_serpwow_client(self) -> None:
        # startup_event must install the semaphore where run_serpwow_search reads it.
        async def scenario() -> None:
            with patch.object(engine, "init_rabbitmq", side_effect=RuntimeError("down")):
                await engine.startup_event()

        original = serpwow_client.search_fetch_semaphore
        try:
            asyncio.run(scenario())
            self.assertIsInstance(
                serpwow_client.search_fetch_semaphore, asyncio.Semaphore
            )
            with patch.dict("os.environ", {"SERPWOW_API_KEY": "test-key"}):
                result = asyncio.run(
                    serpwow_client.run_serpwow_search("acme", client=_FakeClient())
                )
            self.assertIsNone(result.get("error"))
        finally:
            serpwow_client.search_fetch_semaphore = original
            engine.search_fetch_semaphore = None


if __name__ == "__main__":
    unittest.main()
