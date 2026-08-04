"""scrape.do Google Maps client — the gmaps pipeline's provider since 2026-08.

Drives the real client through httpx.MockTransport (no network, no monkeypatching)
using a payload shaped like a live /plugin/google/maps/search response.
"""
import asyncio
import os
import unittest
from unittest import mock

import httpx

from app.services.serpwow import scrapedo_maps_client as client
from app.services.serpwow.gmaps_scoring import _score_gmaps_candidates

TOKEN_ENV = {"SCRAPEDO_TOKEN": "test-token", "SCRAPEDO_MAX_RETRIES": "2",
             "SCRAPEDO_TIMEOUT_SECONDS": "5"}

# Trimmed from a real response: entry #1 has a website, #2 does not (verified against
# /maps/place — a website-less local_result has no website there either).
PAYLOAD = {
    "search_metadata": {"google_maps_url": "https://www.google.com/maps/search/x"},
    "local_results": [
        {"position": 1, "title": "ABC Sac Metal", "data_cid": "142574613",
         "website": "https://abcsacmetal.com.tr/#contact", "phone": "(0312) 581 06 06",
         "address": "Turgut Ozal 2 Blv No:156, Mamak/Ankara", "type": "Metal supplier",
         "types": ["Metal supplier", "Pipe supplier"], "rating": 4.5, "reviews": 56},
        {"position": 2, "title": "AMIGO CORPORATION", "data_cid": "159152473",
         "address": "18 No, Road Baridhara J Block, Dhaka", "type": "Car dealer",
         "types": ["Car dealer"]},
    ],
}


def _run(handler, **env):
    """Run process_gmaps_query against a mock transport; returns the envelope."""
    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            return await client.process_gmaps_query(
                "ABC Sac Metal Ankara", gl="tr", client=http_client)

    with mock.patch.dict(os.environ, {**TOKEN_ENV, **env}, clear=False):
        return asyncio.run(go())


class SuccessTests(unittest.TestCase):
    def test_local_results_pass_through_verbatim_in_order(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=PAYLOAD)

        env = _run(handler)

        # results IS local_results, unmutated — it is persisted as the row artifact.
        self.assertEqual(env["results"], PAYLOAD["local_results"])
        self.assertEqual([r["position"] for r in env["results"]], [1, 2])
        self.assertEqual(env["total_places"], 2)
        self.assertIsNone(env["error"])
        # Request shape: the token, the query, hl=en and the caller's gl.
        self.assertIn("token=test-token", seen["url"])
        self.assertIn("hl=en", seen["url"])
        self.assertIn("gl=tr", seen["url"])

    def test_one_successful_call_costs_ten_credits(self) -> None:
        env = _run(lambda request: httpx.Response(200, json=PAYLOAD))
        self.assertEqual(env["request_count"], 1)
        self.assertEqual(env["credits"], client.CREDITS_PER_CALL)
        self.assertEqual(env["credits"], 10)

    def test_zero_results_is_not_an_error_but_is_flagged_as_billed_empty(self) -> None:
        env = _run(lambda request: httpx.Response(200, json={"local_results": []}))
        self.assertEqual(env["results"], [])
        self.assertIsNone(env["error"])
        # Still a billed call — and that's exactly why it's worth flagging: 10 credits
        # spent for no data is the case to claim back from scrape.do.
        self.assertEqual(env["credits"], 10)
        self.assertTrue(env["billed_empty"])

    def test_a_result_bearing_response_is_not_billed_empty(self) -> None:
        self.assertFalse(_run(lambda r: httpx.Response(200, json=PAYLOAD))["billed_empty"])

    def test_free_502_no_results_is_not_billed_empty(self) -> None:
        # Zero credits spent, so there is nothing to claim back.
        with mock.patch.object(client.asyncio, "sleep", new=_no_sleep):
            env = _run(lambda r: httpx.Response(502, json={"error": "no results"}))
        self.assertTrue(env["no_results"])
        self.assertFalse(env["billed_empty"])
        self.assertEqual(env["credits"], 0)

    def test_scorer_skips_the_website_less_entry(self) -> None:
        env = _run(lambda request: httpx.Response(200, json=PAYLOAD))
        scored = _score_gmaps_candidates(env, "ABC Sac Metal", "Turgut Ozal 2 Blv No:156")
        self.assertEqual([e["url"] for e in scored], ["https://abcsacmetal.com.tr/"])

    def test_extract_gmaps_website_fallback_strips_fragment(self) -> None:
        env = _run(lambda request: httpx.Response(200, json=PAYLOAD))
        self.assertEqual(client.extract_gmaps_website(env), "https://abcsacmetal.com.tr/")


class SharedClientTests(unittest.TestCase):
    """A fresh AsyncClient per row cost ~400ms of DNS/TCP/TLS setup per call (measured
    636ms -> 233ms after pooling), i.e. ~55 hours across 500k rows. These assert the
    pooling actually holds, since every other test injects its own client and would
    never exercise this path."""

    def setUp(self) -> None:
        asyncio.run(client.close_shared_client())

    tearDown = setUp

    def test_same_client_is_reused_across_calls(self) -> None:
        async def go():
            a = await client._get_shared_client()
            b = await client._get_shared_client()
            return a, b

        a, b = asyncio.run(go())
        self.assertIs(a, b)

    def test_close_releases_it_and_a_later_call_rebuilds(self) -> None:
        async def go():
            a = await client._get_shared_client()
            await client.close_shared_client()
            self.assertIsNone(client._shared_client)
            b = await client._get_shared_client()
            return a, b

        a, b = asyncio.run(go())
        self.assertIsNot(a, b)
        self.assertTrue(a.is_closed)

    def test_pool_is_sized_to_the_concurrency_cap(self) -> None:
        """Keepalive must cover every in-flight slot, or slots above the default 20
        would re-handshake and the fix would only half-work."""
        async def go():
            with mock.patch.dict(os.environ, {"SCRAPEDO_CONCURRENCY": "100"}, clear=False):
                from app.services.common import provider_limits
                provider_limits.reset_scrapedo_limit()
                return await client._get_shared_client()

        c = asyncio.run(go())
        self.assertEqual(c._transport._pool._max_connections, 100)
        self.assertEqual(c._transport._pool._max_keepalive_connections, 100)

    def test_a_call_does_not_close_the_shared_client(self) -> None:
        """The old code wrapped the client in `async with`, which would close the
        pooled client after the first row and defeat reuse entirely."""
        pooled = None

        async def go():
            nonlocal pooled
            pooled = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json=PAYLOAD)))
            with mock.patch.object(client, "_get_shared_client",
                                   new=mock.AsyncMock(return_value=pooled)), \
                 mock.patch.dict(os.environ, TOKEN_ENV, clear=False):
                return await client.process_gmaps_query("q", gl="us")

        env = asyncio.run(go())
        self.assertEqual(env["credits"], 10)
        self.assertFalse(pooled.is_closed, "the pooled client must survive the call")
        asyncio.run(pooled.aclose())


class CallAccountingTests(unittest.TestCase):
    """A run must reconcile as "N calls = X succeeded + Y failed", with credits charged
    on the successes only."""

    def test_success_counts_as_one_billed_call(self) -> None:
        env = _run(lambda request: httpx.Response(200, json=PAYLOAD))
        self.assertEqual(env["request_count"], 1)
        self.assertEqual(env["successful_requests"], 1)
        self.assertEqual(env["failed_requests"], 0)
        self.assertEqual(env["credits"], 10)

    def test_retried_failure_counts_every_attempt_as_failed(self) -> None:
        with mock.patch.object(client.asyncio, "sleep", new=_no_sleep):
            env = _run(lambda request: httpx.Response(500, text="boom"),
                       SCRAPEDO_MAX_RETRIES="3")
        self.assertEqual(env["request_count"], 4)
        self.assertEqual(env["successful_requests"], 0)
        self.assertEqual(env["failed_requests"], 4)
        self.assertEqual(env["credits"], 0)

    def test_requests_always_equal_successful_plus_failed(self) -> None:
        for handler, retries in (
            (lambda r: httpx.Response(200, json=PAYLOAD), "3"),
            (lambda r: httpx.Response(500, text="x"), "0"),
            (lambda r: httpx.Response(429, text="x"), "1"),
            (lambda r: httpx.Response(502, json={"error": "no results"}), "3"),
        ):
            with mock.patch.object(client.asyncio, "sleep", new=_no_sleep):
                env = _run(handler, SCRAPEDO_MAX_RETRIES=retries)
            self.assertEqual(env["request_count"],
                             env["successful_requests"] + env["failed_requests"],
                             msg=f"unbalanced: {env}")
            self.assertEqual(env["credits"],
                             client.CREDITS_PER_CALL * env["successful_requests"])


class NoResultsTests(unittest.TestCase):
    """scrape.do returns HTTP 502 {"error": "no results"} when Google has no Maps
    listing. That is a business not-found, not a transient failure — 12/100 rows of a
    real run hit it and were wrongly reported as errors."""

    def test_no_results_is_retried_before_being_believed(self) -> None:
        """502 is overloaded by scrape.do: transient failure AND "no listing". We can't
        tell which, and a failed attempt is unbilled, so spend the retries first."""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(502, json={"error": "no results"})

        with mock.patch.object(client.asyncio, "sleep", new=_no_sleep):
            env = _run(handler, SCRAPEDO_MAX_RETRIES="3")
        self.assertEqual(len(calls), 4, "1 attempt + 3 retries")
        self.assertEqual(env["request_count"], 4)
        # Still unbilled and still a not-found, not an error.
        self.assertTrue(env["no_results"])
        self.assertEqual(env["credits"], 0)
        self.assertIsNone(env["error"])

    def test_a_transient_502_that_recovers_is_billed_once(self) -> None:
        """The reason retrying 502 "no results" is worth it: sometimes it IS transient."""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 3:
                return httpx.Response(502, json={"error": "no results"})
            return httpx.Response(200, json=PAYLOAD)

        with mock.patch.object(client.asyncio, "sleep", new=_no_sleep):
            env = _run(handler, SCRAPEDO_MAX_RETRIES="3")
        self.assertEqual(env["results"], PAYLOAD["local_results"])
        self.assertFalse(env["no_results"])
        self.assertEqual(env["credits"], 10)
        self.assertEqual(env["failed_requests"], 2)

    def test_no_results_is_not_an_error(self) -> None:
        with mock.patch.object(client.asyncio, "sleep", new=_no_sleep):
            env = _run(lambda request: httpx.Response(502, json={"error": "no results"}))
        self.assertIsNone(env["error"])
        self.assertIsNone(env["error_category"])
        self.assertTrue(env["no_results"])
        self.assertEqual(env["results"], [])

    def test_no_results_is_not_billed(self) -> None:
        # Retried now (502 is overloaded), so every attempt shows as a failed call —
        # but none of them is billed, which is the point.
        with mock.patch.object(client.asyncio, "sleep", new=_no_sleep):
            env = _run(lambda request: httpx.Response(502, json={"error": "no results"}),
                       SCRAPEDO_MAX_RETRIES="2")
        self.assertEqual(env["credits"], 0)
        self.assertEqual(env["successful_requests"], 0)
        self.assertEqual(env["failed_requests"], 3)
        self.assertEqual(env["request_count"], 3)

    def test_transient_502_is_still_retried(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(502, json={"error": "request failed"})

        with mock.patch.object(client.asyncio, "sleep", new=_no_sleep):
            env = _run(handler, SCRAPEDO_MAX_RETRIES="3")
        self.assertEqual(len(calls), 4, "a real 502 must still retry")
        self.assertFalse(env["no_results"])
        self.assertEqual(env["error_category"], "http_5xx")

    def test_transient_502_that_recovers(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 3:
                return httpx.Response(502, json={"error": "request failed"})
            return httpx.Response(200, json=PAYLOAD)

        with mock.patch.object(client.asyncio, "sleep", new=_no_sleep):
            env = _run(handler, SCRAPEDO_MAX_RETRIES="3")
        self.assertEqual(env["request_count"], 3)
        self.assertEqual(env["successful_requests"], 1)
        self.assertEqual(env["failed_requests"], 2)
        self.assertEqual(env["credits"], 10)  # billed once, not three times
        self.assertEqual(env["results"], PAYLOAD["local_results"])


class BackoffTests(unittest.TestCase):
    def test_default_is_two_retries(self) -> None:
        calls = []
        with mock.patch.dict(os.environ, {"SCRAPEDO_MAX_RETRIES": ""}, clear=False), \
             mock.patch.object(client.asyncio, "sleep", new=_no_sleep):
            def handler(request: httpx.Request) -> httpx.Response:
                calls.append(1)
                return httpx.Response(500, text="x")

            async def go():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                    return await client.process_gmaps_query("q", gl="us", client=c)

            with mock.patch.dict(os.environ, {"SCRAPEDO_TOKEN": "t"}, clear=False):
                asyncio.run(go())
        self.assertEqual(len(calls), 3, "1 initial attempt + 2 retries")

    def test_rate_limit_backs_off_harder_than_5xx(self) -> None:
        # A 1s/2s ramp exhausted every attempt on 27/50 rows of a live run.
        self.assertEqual(client._backoff_seconds(0, 500, None), 1.0)
        self.assertEqual(client._backoff_seconds(1, 500, None), 2.0)
        self.assertEqual(client._backoff_seconds(2, 500, None), 4.0)
        self.assertEqual(client._backoff_seconds(0, 429, None), 5.0)
        self.assertEqual(client._backoff_seconds(1, 429, None), 10.0)
        self.assertEqual(client._backoff_seconds(2, 429, None), 20.0)

    def test_retry_after_header_wins_but_is_capped(self) -> None:
        self.assertEqual(client._backoff_seconds(0, 429, "7"), 7.0)
        self.assertEqual(client._backoff_seconds(0, 429, "9999"), client.MAX_BACKOFF_SECONDS)
        # Garbage falls back to the computed backoff rather than crashing.
        self.assertEqual(client._backoff_seconds(0, 429, "soon"), 5.0)

    def test_backoff_is_capped(self) -> None:
        self.assertLessEqual(client._backoff_seconds(20, 429, None), client.MAX_BACKOFF_SECONDS)


class FailureTests(unittest.TestCase):
    def test_failed_calls_cost_no_credits(self) -> None:
        env = _run(lambda request: httpx.Response(500, text="boom"),
                   SCRAPEDO_MAX_RETRIES="0")
        self.assertEqual(env["credits"], 0)
        self.assertEqual(env["results"], [])
        self.assertEqual(env["error_category"], "http_5xx")
        self.assertIn("500", env["error"])

    def test_4xx_is_not_retried(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(403, json={"error": "forbidden"})

        env = _run(handler, SCRAPEDO_MAX_RETRIES="2")
        self.assertEqual(len(calls), 1)
        self.assertEqual(env["error_category"], "auth")

    def test_missing_token_never_touches_the_network(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made without a token")

        env = _run(handler, SCRAPEDO_TOKEN="")
        self.assertEqual(env["error_category"], "auth")
        self.assertEqual(env["request_count"], 0)
        self.assertEqual(env["credits"], 0)

    def test_error_body_on_http_200_is_billed_but_reported(self) -> None:
        env = _run(lambda request: httpx.Response(200, json={"error": "quota exceeded"}))
        self.assertIn("quota exceeded", env["error"])
        # HTTP 200 means scrape.do charged for it.
        self.assertEqual(env["credits"], 10)

    def test_token_in_a_JSON_error_body_is_redacted(self) -> None:
        """_safe_error's JSON-body branch was the one path that skipped _redact (the
        non-JSON fallback always had it), so a provider error that echoes the request URL
        persisted the API token into a durable per-row S3 error object."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": f"bad token for {request.url}"})

        env = _run(handler, SCRAPEDO_MAX_RETRIES="0")
        self.assertNotIn("test-token", env["error"])
        self.assertIn("[REDACTED]", env["error"])
        # gmaps keeps the default endpoint label — nothing about this pipeline changed.
        self.assertIn("scrape.do maps search failed", env["error"])

    def test_token_is_redacted_from_error_text(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed connecting to {request.url}")

        env = _run(handler, SCRAPEDO_MAX_RETRIES="0")
        self.assertNotIn("test-token", env["error"])
        self.assertIn("[REDACTED]", env["error"])
        self.assertEqual(env["error_category"], "network")


async def _no_sleep(_seconds):
    return None


if __name__ == "__main__":
    unittest.main()
