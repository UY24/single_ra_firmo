# backend/app/services/serpwow/scrapedo_ai_client.py
"""scrape.do Google AI Mode client for the relationship pipeline.

One call per row: the whole research prompt goes out as ``q=`` and the provider
returns Google's AI Mode answer as ``text_blocks[]`` plus its cited ``references[]``.

Deliberately imports the pooled client, backoff and redaction helpers from
``scrapedo_maps_client`` rather than duplicating them — same vendor, same retry
semantics, and gmaps stays untouched. If a third scrape.do endpoint ever appears,
extract the shared half then.

Billing: CREDITS_PER_CALL per *successful* (HTTP 200) call. Failed attempts are free,
which is why retrying costs only latency.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import httpx

from app.services.common.env import get_int_env as _get_int_env
from app.services.common.provider_limits import scrapedo_slot
from app.services.serpwow.outcomes import categorize_http_error
from app.services.serpwow.scrapedo_maps_client import (
    CREDITS_PER_CALL,
    _backoff_seconds,
    _get_shared_client,
    _redact,
    _safe_error,
)

AI_MODE_SEARCH_URL = "https://api.scrape.do/plugin/google/search/ai-mode"


def _envelope(
    query: str,
    gl: str,
    *,
    request_count: int = 0,
    successful_requests: int = 0,
    response: Optional[dict[str, Any]] = None,
    response_text: Optional[str] = None,
    error: Optional[str] = None,
    error_category: Optional[str] = None,
    billed_empty: bool = False,
) -> dict[str, Any]:
    """The envelope the relationship row executor consumes.

    ``response`` is scrape.do's decoded body, for in-process logic. ``response_text`` is
    the response body EXACTLY as it came off the wire — that string, not a re-serialised
    copy of it, is what gets written to ``raw/``, so the object in the bucket is
    byte-for-byte what scrape.do sent. Everything else here is call bookkeeping the
    provider does not report, and it is stored separately (see s3_run_store).

    ``credits`` is DERIVED from the HTTP-200 count, never counted by hand, so a run
    always reconciles as ``request_count == successful_requests + failed_requests``.
    """
    return {
        "query": query,
        "gl": gl,
        "request_count": request_count,
        "successful_requests": successful_requests,
        "failed_requests": max(0, request_count - successful_requests),
        "credits": CREDITS_PER_CALL * successful_requests,
        "response": response if isinstance(response, dict) else None,
        "response_text": response_text,
        # A BILLED (200) call that returned no text and no references: credits spent for
        # no data. Counted for the scrape.do refund claim; never retried, since the money
        # is already gone and a second call cannot be told apart from the first.
        "billed_empty": billed_empty,
        "error": error,
        "error_category": error_category,
    }


async def search_ai_mode(
    query: str,
    gl: str = "us",
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """One Google AI Mode search via scrape.do. Never raises — errors come back in the
    envelope so the caller can map them onto the row outcome taxonomy."""
    token = os.getenv("SCRAPEDO_TOKEN", "").strip()
    if not token:
        return _envelope(
            query, gl, error="SCRAPEDO_TOKEN is not configured", error_category="auth")

    # Reuse the pooled keep-alive client unless a caller injects one (tests pass a
    # MockTransport-backed client). Deliberately NOT `async with`: the shared client
    # must outlive this call.
    if client is None:
        client = await _get_shared_client()

    params = {"token": token, "q": query, "hl": "en", "gl": gl}
    # Retries AFTER the first attempt: 3 => 4 calls per row.
    attempts = max(1, _get_int_env("SCRAPEDO_MAX_RETRIES", 3) + 1)
    request_count = 0

    for attempt in range(attempts):
        response = None
        try:
            request_count += 1
            # Account-wide scrape.do gate, shared with gmaps and AI Mode. Wraps only the
            # HTTP call so a slot is never held across a backoff sleep.
            async with scrapedo_slot():
                response = await client.get(AI_MODE_SEARCH_URL, params=params)
            response.raise_for_status()
            body_text = response.text
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
                query, gl,
                request_count=request_count,
                # label: this string is persisted per row and shown in "View failed rows"
                # — without it every transport error/429/5xx on this pipeline reads as a
                # Google MAPS failure (the shared helper's default).
                error=_safe_error(exc, response, label="ai-mode"),
                error_category=categorize_http_error(
                    status, f"{type(exc).__name__}: {exc}"),
            )

        # HTTP 200 == a billed call, even when the body then reports a problem. The body
        # is still kept: it is what the provider actually sent for the credits spent.
        if isinstance(payload, dict) and payload.get("error"):
            message = _redact(payload["error"])
            return _envelope(
                query, gl,
                request_count=request_count,
                successful_requests=1,
                response=payload,
                response_text=body_text,
                error=f"scrape.do ai-mode search failed: {message}",
                error_category=categorize_http_error(None, message),
            )

        body = payload if isinstance(payload, dict) else {}
        blocks = body.get("text_blocks")
        refs = body.get("references")
        return _envelope(
            query, gl,
            request_count=request_count,
            successful_requests=1,
            response=body,
            response_text=body_text,
            billed_empty=not (blocks if isinstance(blocks, list) else [])
                         and not (refs if isinstance(refs, list) else []),
        )

    # Unreachable: the loop either returns or exhausts into the error path above.
    return _envelope(query, gl, request_count=request_count,
                     error="scrape.do ai-mode search failed", error_category="internal")
