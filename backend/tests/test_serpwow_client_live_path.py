# Regression tests for the engine.py decomposition: run_serpwow_search must be
# executable end-to-end (HTTP mocked) without engine.startup_event() having run.
# The 2026-07-07 split left `search_fetch_semaphore` defined only in engine.py,
# so every LIVE search died with a swallowed NameError while the mocked suite
# stayed green. These tests exercise the real function body.
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.services.serpwow import engine, serpwow_client

class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "https://api.serpwow.com"),
                response=httpx.Response(self.status_code),
            )

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


class _SequenceClient:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = 0

    async def get(self, url, params=None):
        self.calls += 1
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


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

    def test_retries_timeout_twice_then_succeeds(self) -> None:
        client = _SequenceClient([
            httpx.ReadTimeout("timed out"),
            httpx.ReadTimeout("timed out"),
            _FakeResponse(),
        ])
        with (
            patch.dict("os.environ", {"SERPWOW_API_KEY": "test-key"}),
            patch.object(asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            result = asyncio.run(
                serpwow_client.run_serpwow_search("acme", client=client)
            )
        self.assertTrue(result["used"])
        self.assertEqual(client.calls, 3)
        self.assertEqual(sleep.await_count, 2)

    def test_retries_http_503_twice_then_succeeds(self) -> None:
        client = _SequenceClient([
            _FakeResponse(503),
            _FakeResponse(503),
            _FakeResponse(),
        ])
        with (
            patch.dict("os.environ", {"SERPWOW_API_KEY": "test-key"}),
            patch.object(asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            result = asyncio.run(
                serpwow_client.run_serpwow_search("acme", client=client)
            )
        self.assertTrue(result["used"])
        self.assertEqual(client.calls, 3)
        self.assertEqual(sleep.await_count, 2)

    def test_does_not_retry_http_403(self) -> None:
        client = _SequenceClient([_FakeResponse(403)])
        with (
            patch.dict("os.environ", {"SERPWOW_API_KEY": "test-key"}),
            patch.object(asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            result = asyncio.run(
                serpwow_client.run_serpwow_search("acme", client=client)
            )
        self.assertFalse(result["used"])
        self.assertEqual(result["error_category"], "auth")
        self.assertEqual(client.calls, 1)
        sleep.assert_not_awaited()

    def test_empty_timeout_message_keeps_exception_name(self) -> None:
        client = _SequenceClient([httpx.ReadTimeout("")] * 3)
        with (
            patch.dict("os.environ", {"SERPWOW_API_KEY": "test-key"}),
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            result = asyncio.run(
                serpwow_client.run_serpwow_search("acme", client=client)
            )
        self.assertIn("ReadTimeout", result["error"])
        self.assertEqual(result["error_category"], "timeout")
        self.assertEqual(client.calls, 3)


if __name__ == "__main__":
    unittest.main()
