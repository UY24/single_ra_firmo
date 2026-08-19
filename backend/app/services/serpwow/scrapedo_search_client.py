# backend/app/services/serpwow/scrapedo_search_client.py
"""scrape.do Google Search client for the firmographics pipeline.

Replaces the SerpWow ``/live/search?include_ai_overview=true`` call (``codetails.py``).
Two endpoints, because Google does not always have the AI Overview ready when the SERP
is served:

1. ``/plugin/google/search`` — the SERP as JSON. Carries ``ai_overview`` inline when
   ``ai_overview.state == "complete"``, plus ``knowledge_graph`` / ``local_results`` /
   ``organic_results`` that the old SerpWow call threw away. **10 credits** per HTTP 200.
2. ``/plugin/google/search/ai-overview?session_key=…`` — the deferred overview, fetched
   only when step 1 answered ``state == "deferred"``. **5 credits** per HTTP 200.

``state`` is also ``null``/absent, meaning Google produced no overview for this query at
all. That is a billed search with nothing to extract, not a failure — see
``billed_no_overview``.

Billing is CREDITS, never USD: credits are charged on HTTP 200s only, so failed attempts
are free and a retry costs nothing but latency. The run's USD figure covers the LLM alone.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Optional

import httpx

from app.services.common.env import (
    get_float_env as _get_float_env,
    get_int_env as _get_int_env,
)
from app.services.common.provider_limits import scrapedo_limit, scrapedo_slot
from app.services.serpwow.outcomes import categorize_http_error

SCRAPEDO_SEARCH_URL = "https://api.scrape.do/plugin/google/search"
SCRAPEDO_AI_OVERVIEW_URL = "https://api.scrape.do/plugin/google/search/ai-overview"

# scrape.do's published prices. Not env vars: their pricing, not a per-deployment knob.
CREDITS_PER_SEARCH_CALL = 10
CREDITS_PER_AI_OVERVIEW_CALL = 5

# Cap for any single backoff sleep, so a hostile Retry-After can't park a worker.
MAX_BACKOFF_SECONDS = 30.0

# Values of ai_overview.state we act on. Anything else (including absent) means "no
# overview for this query".
STATE_COMPLETE = "complete"
STATE_DEFERRED = "deferred"


# One AsyncClient reused across rows, sized to the shared scrape.do concurrency cap so
# every in-flight slot holds a warm connection. A fresh client per row costs ~330ms of
# DNS/TCP/TLS on every call (measured on the Maps endpoint) and forfeits keep-alive.
_shared_client: Optional[httpx.AsyncClient] = None
_shared_client_loop: Any = None


async def _get_shared_client() -> httpx.AsyncClient:
    global _shared_client, _shared_client_loop
    loop = asyncio.get_running_loop()
    if (_shared_client is None or _shared_client.is_closed
            or _shared_client_loop is not loop):
        if _shared_client is not None and not _shared_client.is_closed:
            try:
                await _shared_client.aclose()
            except Exception:
                pass
        pool = max(1, scrapedo_limit())
        _shared_client = httpx.AsyncClient(
            timeout=_get_float_env("SCRAPEDO_TIMEOUT_SECONDS", 90.0),
            limits=httpx.Limits(max_connections=pool,
                                max_keepalive_connections=pool,
                                keepalive_expiry=60.0),
        )
        _shared_client_loop = loop
    return _shared_client


async def close_shared_client() -> None:
    """Release the pooled connections (called from the app/worker shutdown hook)."""
    global _shared_client, _shared_client_loop
    if _shared_client is not None and not _shared_client.is_closed:
        try:
            await _shared_client.aclose()
        except Exception:
            pass
    _shared_client = None
    _shared_client_loop = None


def _redact(value: Any) -> str:
    """Strip the API token out of anything we put in an error message or log."""
    return re.sub(r"([?&]token=)[^&'\"\s]+", r"\1[REDACTED]", str(value or ""))


def _response_error_text(response: Any) -> str:
    """The provider's own error string from a JSON body, or "" if there isn't one."""
    try:
        payload = response.json()
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("error") or payload.get("message") or "").strip()


def _safe_error(exc: Exception, response: Any = None, label: str = "search") -> str:
    """A durable, token-free error string for one failed scrape.do call."""
    status = getattr(response, "status_code", None)
    if status is not None:
        body = _redact(_response_error_text(response)).strip()
        if not body:
            try:
                body = _redact(response.text)[:200]
            except Exception:
                body = ""
        if body:
            return f"scrape.do {label} failed (HTTP {status}): {body.rstrip('.')}."
        return f"scrape.do {label} failed (HTTP {status})."
    return _redact(exc) or type(exc).__name__


def _backoff_seconds(attempt: int, status: Optional[int], retry_after: Any) -> float:
    """Exponential backoff with a 429-specific base, matching the Maps client: rate
    limits need real room, a transient 5xx usually clears immediately, and Retry-After
    wins when the server sends one."""
    if retry_after:
        try:
            return min(float(str(retry_after).strip()), MAX_BACKOFF_SECONDS)
        except (TypeError, ValueError):
            pass
    base = 5.0 if status == 429 else 1.0
    return min(base * (2 ** attempt), MAX_BACKOFF_SECONDS)


def _ai_overview_state(payload: Any) -> Optional[str]:
    """``ai_overview.state`` lowercased, or None when there is no overview block."""
    if not isinstance(payload, dict):
        return None
    block = payload.get("ai_overview")
    if not isinstance(block, dict):
        return None
    state = block.get("state")
    return str(state).strip().lower() if state else None


def _session_key(payload: Any) -> str:
    """The deferred follow-up key. Checked inside ``ai_overview`` first, then top level,
    because the provider documents the field without pinning where it sits."""
    if not isinstance(payload, dict):
        return ""
    block = payload.get("ai_overview")
    if isinstance(block, dict) and block.get("session_key"):
        return str(block["session_key"]).strip()
    return str(payload.get("session_key") or "").strip()


def _envelope(
    query: str,
    gl: str,
    *,
    search_requests: int = 0,
    search_successful: int = 0,
    ai_overview_requests: int = 0,
    ai_overview_successful: int = 0,
    ai_overview: Optional[dict[str, Any]] = None,
    ai_overview_state: Optional[str] = None,
    ai_overview_error: Optional[str] = None,
    deferred: bool = False,
    raw_response: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
    error_category: Optional[str] = None,
) -> dict[str, Any]:
    """The envelope ``modes/firmographics`` consumes.

    Call accounting is kept per ENDPOINT because the two are priced differently, then
    summed — so a run can always answer "what did the credits buy?":
    ``credits = 10 × search 200s + 5 × ai-overview 200s``, derived, never hand-counted.
    Every attempt counts toward ``request_count``; only 200s are billed.
    """
    request_count = search_requests + ai_overview_requests
    successful = search_successful + ai_overview_successful
    credits = (CREDITS_PER_SEARCH_CALL * search_successful
               + CREDITS_PER_AI_OVERVIEW_CALL * ai_overview_successful)
    return {
        "provider": "scrapedo",
        "used": bool(search_successful),
        "query": query,
        "gl": gl,
        "ai_overview": ai_overview,
        # "complete" | "deferred" | None (Google produced no overview for this query).
        "ai_overview_state": ai_overview_state,
        # Set when the deferred FOLLOW-UP failed. Not a row error: the search itself was
        # billed and its SERP is intact, we just have no overview to normalise.
        "ai_overview_error": ai_overview_error,
        # "Google deferred this row, so we paid for a follow-up" — NOT derived from the
        # final state, because a follow-up that succeeds rewrites the state to "complete"
        # and would hide the 5 credits it cost.
        "deferred": deferred,
        # A billed search that yielded no usable overview — the credits-for-nothing case
        # (refund claim), whether Google had none or the deferred fetch failed.
        "billed_no_overview": bool(search_successful) and not ai_overview,
        "raw_response": raw_response,
        "search_requests": search_requests,
        "search_successful": search_successful,
        "ai_overview_requests": ai_overview_requests,
        "ai_overview_successful": ai_overview_successful,
        "request_count": request_count,
        "successful_requests": successful,
        "failed_requests": max(0, request_count - successful),
        "credits": credits,
        "error": error,
        "error_category": error_category,
    }


async def _fetch_deferred_ai_overview(
    client: httpx.AsyncClient,
    token: str,
    session_key: str,
) -> tuple[Optional[dict[str, Any]], int, int, Optional[str]]:
    """One follow-up call. Returns (overview, requests, successful, error).

    **Deliberately never retried.** The session key is single-use and expires after 60
    seconds; a second attempt with the same key gets HTTP 404 ``session not found``, so a
    retry cannot succeed and would only add latency to a row that already has its SERP.
    """
    response = None
    try:
        async with scrapedo_slot():
            response = await client.get(
                SCRAPEDO_AI_OVERVIEW_URL,
                params={"token": token, "session_key": session_key},
            )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return None, 1, 0, _safe_error(exc, response, label="ai-overview fetch")

    # HTTP 200 == billed, even when the body then reports a problem.
    if isinstance(payload, dict) and payload.get("error"):
        return None, 1, 1, f"scrape.do ai-overview fetch failed: {_redact(payload['error'])}"
    block = payload.get("ai_overview") if isinstance(payload, dict) else None
    if not isinstance(block, dict):
        # Some shapes return the overview at the top level rather than nested.
        block = payload if isinstance(payload, dict) else None
    return (block if isinstance(block, dict) else None), 1, 1, None


async def search_with_ai_overview(
    q: str,
    gl: str = "us",
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """One Google Search via scrape.do, plus the deferred AI-Overview fetch when needed.

    Never raises — failures come back in the envelope so the caller maps them onto the
    row outcome taxonomy.
    """
    token = os.getenv("SCRAPEDO_TOKEN", "").strip()
    if not token:
        return _envelope(q, gl, error="SCRAPEDO_TOKEN is not configured",
                         error_category="auth")

    # Reuse the pooled client unless a caller injects one (tests pass a MockTransport).
    # Deliberately NOT an `async with`: the shared client must outlive this call.
    if client is None:
        client = await _get_shared_client()

    params = {"token": token, "q": q, "hl": "en", "gl": gl}
    # Retries AFTER the first attempt, so 2 => 3 calls per row. Shares the app-wide
    # SCRAPEDO_MAX_RETRIES with gmaps and AI Mode: one scrape.do retry knob.
    attempts = max(1, _get_int_env("SCRAPEDO_MAX_RETRIES", 2) + 1)
    search_requests = 0

    for attempt in range(attempts):
        response = None
        try:
            search_requests += 1
            # Account-wide scrape.do gate. Wraps only the HTTP call so a slot is never
            # held across a backoff sleep.
            async with scrapedo_slot():
                response = await client.get(SCRAPEDO_SEARCH_URL, params=params)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            status = getattr(response, "status_code", None)
            retryable = (
                isinstance(exc, httpx.TransportError)
                or status == 429
                or (status is not None and 500 <= status <= 599)
            )
            if retryable and attempt < attempts - 1:
                retry_after = (response.headers.get("Retry-After")
                               if response is not None else None)
                await asyncio.sleep(_backoff_seconds(attempt, status, retry_after))
                continue
            return _envelope(
                q, gl,
                search_requests=search_requests,
                error=_safe_error(exc, response),
                error_category=categorize_http_error(
                    status, f"{type(exc).__name__}: {exc}"),
            )

        # From here the search call was an HTTP 200 and IS billed.
        if isinstance(payload, dict) and payload.get("error"):
            message = _redact(payload["error"])
            return _envelope(
                q, gl,
                search_requests=search_requests,
                search_successful=1,
                raw_response=payload if isinstance(payload, dict) else None,
                error=f"scrape.do search failed: {message}",
                error_category=categorize_http_error(None, message),
            )

        state = _ai_overview_state(payload)
        overview = payload.get("ai_overview") if isinstance(payload, dict) else None
        overview = overview if isinstance(overview, dict) else None
        deferred = state == STATE_DEFERRED
        aio_requests = 0
        aio_successful = 0
        aio_error: Optional[str] = None

        if deferred:
            # The inline block on a deferred row is a STUB — {state, session_key} and no
            # content. Drop it: leaving it in place made `billed_no_overview` False (the
            # dict is non-empty) and would have fed the stub to the LLM as if it were an
            # overview. Only what the follow-up returns counts as content.
            overview = None
            session_key = _session_key(payload)
            if session_key:
                fetched, aio_requests, aio_successful, aio_error = (
                    await _fetch_deferred_ai_overview(client, token, session_key))
                if fetched:
                    overview = fetched
                    state = STATE_COMPLETE
            else:
                aio_error = ("scrape.do reported a deferred AI overview "
                             "but returned no session_key")

        return _envelope(
            q, gl,
            search_requests=search_requests,
            search_successful=1,
            ai_overview_requests=aio_requests,
            ai_overview_successful=aio_successful,
            ai_overview=overview,
            ai_overview_state=state,
            ai_overview_error=aio_error,
            deferred=deferred,
            raw_response=payload if isinstance(payload, dict) else None,
        )

    # Unreachable: the loop either returns or exhausts into the error path above.
    return _envelope(q, gl, search_requests=search_requests,
                     error="scrape.do search failed", error_category="internal")
