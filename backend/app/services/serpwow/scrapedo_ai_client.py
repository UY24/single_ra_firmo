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
    text_blocks: Optional[list[Any]] = None,
    references: Optional[list[Any]] = None,
    error: Optional[str] = None,
    error_category: Optional[str] = None,
    billed_empty: bool = False,
) -> dict[str, Any]:
    """The envelope the relationship row executor consumes.

    ``text_blocks``/``references`` are the provider's arrays VERBATIM: this envelope is
    persisted as the row's durable artifact, so it must stay faithful.

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
        "text_blocks": text_blocks if isinstance(text_blocks, list) else [],
        "references": references if isinstance(references, list) else [],
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
                error=_safe_error(exc, response),
                error_category=categorize_http_error(
                    status, f"{type(exc).__name__}: {exc}"),
            )

        # HTTP 200 == a billed call, even when the body then reports a problem.
        if isinstance(payload, dict) and payload.get("error"):
            message = _redact(payload["error"])
            return _envelope(
                query, gl,
                request_count=request_count,
                successful_requests=1,
                error=f"scrape.do ai-mode search failed: {message}",
                error_category=categorize_http_error(None, message),
            )

        blocks = payload.get("text_blocks") if isinstance(payload, dict) else None
        refs = payload.get("references") if isinstance(payload, dict) else None
        blocks = blocks if isinstance(blocks, list) else []
        refs = refs if isinstance(refs, list) else []
        return _envelope(
            query, gl,
            request_count=request_count,
            successful_requests=1,
            text_blocks=blocks,
            references=refs,
            billed_empty=not blocks and not refs,
        )

    # Unreachable: the loop either returns or exhausts into the error path above.
    return _envelope(query, gl, request_count=request_count,
                     error="scrape.do ai-mode search failed", error_category="internal")
