# backend/app/services/serpwow/scrapedo_maps_client.py
"""scrape.do Google Maps search client for the gmaps pipeline.

Replaces the SerpWow places + place_details pair (the deleted ``gmaps_client.py``).
scrape.do's ``/plugin/google/maps/search`` returns ``website``/``title``/``address``/
``phone``/``rating``/``reviews``/``type`` inline on every ``local_results[]`` entry —
the whole set this pipeline consumes — so the old ``1 + N`` place-details fan-out is
gone and a row costs exactly ONE request.

Verified 2026-08-03 against real CSV rows: a ``local_result`` with no ``website`` is
still website-less in ``/plugin/google/maps/place``, so there is nothing a hydration
call could recover. That endpoint is deliberately unused.

Billing: scrape.do charges CREDITS_PER_CALL per *successful* call; failed attempts are
free, which is why retries are cheap and ``credits`` counts HTTP-200s, not attempts.
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

SCRAPEDO_MAPS_SEARCH_URL = "https://api.scrape.do/plugin/google/maps/search"

# scrape.do's published price for a Google Maps call. Not an env var: it's their
# pricing, not a per-deployment setting.
CREDITS_PER_CALL = 10

# scrape.do overloads HTTP 502: it is usually transient ("request failed"), but it also
# means "Google Maps has no listing for this query" — observed on 12/100 real rows, body
# {"error": "no results"}. That is a business NOT-FOUND, not a failure: retrying can
# never succeed, so match the body and return an empty result set instead.
NO_RESULT_MARKERS = ("no results", "no result found")

# Cap for any single backoff sleep, so a hostile Retry-After can't park a worker.
MAX_BACKOFF_SECONDS = 30.0


# One AsyncClient reused across rows. A fresh client per row costs ~330ms of DNS/TCP/TLS
# setup on every call (measured) and forfeits keep-alive entirely — at 500k rows that is
# ~46 hours of pure connection overhead. Keepalive is sized to the concurrency cap so
# every in-flight slot can hold a warm connection instead of re-handshaking.
_shared_client: Optional[httpx.AsyncClient] = None
_shared_client_loop: Any = None


async def _get_shared_client() -> httpx.AsyncClient:
    global _shared_client, _shared_client_loop
    loop = asyncio.get_running_loop()
    # Rebuild if closed, or if we somehow moved loops (tests, restarted worker): an
    # AsyncClient is bound to the loop whose connections it holds.
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


def _safe_error(exc: Exception, response: Any = None, label: str = "maps") -> str:
    """A durable, token-free error string for one failed scrape.do call.

    ``label`` names the ENDPOINT ("maps", "ai-mode"): this string is persisted as the
    row's error object and shown in "View failed rows", so a shared default would report
    every relationship-pipeline transport error as a Google Maps failure. Defaulting to
    "maps" keeps gmaps (and its tests) byte-identical.
    """
    status = getattr(response, "status_code", None)
    if status is not None:
        body = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                # _redact, exactly like the non-JSON fallback below: the provider echoes
                # the request URL (token and all) in some error bodies, and this string
                # is written to a durable per-row S3 object.
                body = _redact(payload.get("error") or payload.get("message") or "").strip()
        except Exception:
            body = _redact(response.text)[:200]
        if body:
            return f"scrape.do {label} search failed (HTTP {status}): {body.rstrip('.')}."
        return f"scrape.do {label} search failed (HTTP {status})."
    return _redact(exc) or type(exc).__name__


def _response_error_text(response: Any) -> str:
    """The provider's own error string from a JSON body, or "" if there isn't one."""
    try:
        payload = response.json()
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("error") or payload.get("message") or "").strip()


def _is_no_results(status: Optional[int], error_text: str) -> bool:
    """A 502 whose body says "no results" — Google has no listing, so don't retry."""
    if status != 502:
        return False
    low = error_text.lower()
    return any(marker in low for marker in NO_RESULT_MARKERS)


def _backoff_seconds(attempt: int, status: Optional[int], retry_after: Any) -> float:
    """Exponential backoff. A 429 gets a much larger base than a 5xx: rate limits need
    real room (a 1s/2s ramp exhausted every attempt on 27/50 rows in a live run), while
    a transient 5xx usually clears immediately. Retry-After wins when the server sends it.
    """
    if retry_after:
        try:
            return min(float(str(retry_after).strip()), MAX_BACKOFF_SECONDS)
        except (TypeError, ValueError):
            pass
    base = 5.0 if status == 429 else 1.0
    return min(base * (2 ** attempt), MAX_BACKOFF_SECONDS)


def clean_url_for_report(url: str) -> str:
    """Strip fragment/text anchors that pollute stored URLs."""
    value = (url or "").strip()
    if not value:
        return ""
    value = re.sub(r"#:~:text=.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"%23:~:text=.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"#.*$", "", value)
    return value.strip()


def extract_gmaps_website(gmaps_results: dict[str, Any]) -> Optional[str]:
    """First non-empty ``website`` across the results.

    Positional fallback for when the scorer likes none of the candidates — same role
    the SerpWow client's version played.
    """
    for item in (gmaps_results or {}).get("results") or []:
        if not isinstance(item, dict):
            continue
        website = clean_url_for_report(item.get("website") or "")
        if website:
            return website
    return None


def _envelope(
    query: str,
    gl: str,
    *,
    request_count: int = 0,
    successful_requests: int = 0,
    results: Optional[list[Any]] = None,
    error: Optional[str] = None,
    error_category: Optional[str] = None,
    no_results: bool = False,
    billed_empty: bool = False,
) -> dict[str, Any]:
    """The envelope gmaps_scoring / run_gmaps_from_module expect.

    ``results`` holds scrape.do's ``local_results[]`` VERBATIM: it is persisted as the
    row's raw response artifact, so it must stay faithful to what the API returned.

    Call accounting, so a run can report "N calls = X succeeded + Y failed":
    ``request_count`` is every HTTP attempt, ``successful_requests`` is the HTTP 200s
    (the only billed ones), ``failed_requests`` is the rest, and ``credits`` is derived
    as ``CREDITS_PER_CALL × successful_requests`` — never counted by hand.
    """
    results = results if isinstance(results, list) else []
    return {
        "query": query,
        "gl": gl,
        "request_count": request_count,
        "successful_requests": successful_requests,
        "failed_requests": max(0, request_count - successful_requests),
        "credits": CREDITS_PER_CALL * successful_requests,
        "total_places": len(results),
        "results": results,
        # True when Google has no Maps listing at all (scrape.do 502 "no results").
        # A business not-found, NOT an error — see NO_RESULT_MARKERS. Costs nothing.
        "no_results": no_results,
        # True when a BILLED (HTTP 200) call came back with zero results — credits
        # spent for no data, i.e. the refund-claim case. `no_results` is the free one.
        "billed_empty": billed_empty,
        "error": error,
        "error_category": error_category,
    }


async def process_gmaps_query(
    q: str,
    gl: str = "us",
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """One Google Maps search via scrape.do. Never raises — errors come back in the
    envelope so the caller can map them onto the row outcome taxonomy."""
    token = os.getenv("SCRAPEDO_TOKEN", "").strip()
    if not token:
        return _envelope(
            q, gl, error="SCRAPEDO_TOKEN is not configured", error_category="auth")

    # Reuse the pooled client (keep-alive) unless a caller injects one — tests pass a
    # MockTransport-backed client. Deliberately NOT an `async with`: the shared client
    # must outlive this call.
    if client is None:
        client = await _get_shared_client()

    params = {"token": token, "q": q, "hl": "en", "gl": gl}
    # Retries AFTER the first attempt, so 2 => 3 calls per row. Shares AI Mode's
    # SCRAPEDO_MAX_RETRIES on purpose — one scrape.do retry knob for the whole app.
    # Failed attempts are NOT billed, so a retry costs only latency.
    attempts = max(1, _get_int_env("SCRAPEDO_MAX_RETRIES", 2) + 1)
    request_count = 0

    for attempt in range(attempts):
        response = None
        try:
            request_count += 1
            # Account-wide scrape.do gate, shared with AI Mode. Wraps only the HTTP
            # call so a slot is never held across backoff sleeps.
            async with scrapedo_slot():
                response = await client.get(SCRAPEDO_MAPS_SEARCH_URL, params=params)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            status = getattr(response, "status_code", None)
            body_error = _response_error_text(response) if response is not None else ""

            # 5xx (incl. 502) / 429 / transport are all retried — scrape.do documents them
            # as transient, and a failed attempt costs no credits, so the only price is
            # latency. A 502 "no results" IS retried too: the same status is overloaded
            # for "transiently broken" and "Google has no listing", and we can't tell
            # which until the retries are spent.
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

            # Attempts exhausted. If it is STILL 502 "no results", treat it as a business
            # not-found (unbilled, no error) rather than a technical failure — retrying
            # proved it wasn't transient.
            if _is_no_results(status, body_error):
                return _envelope(q, gl, request_count=request_count,
                                 results=[], no_results=True)
            return _envelope(
                q, gl,
                request_count=request_count,
                error=_safe_error(exc, response),
                error_category=categorize_http_error(
                    status, f"{type(exc).__name__}: {exc}"),
            )

        # HTTP 200 == a billed call, even if the body then reports a problem.
        if isinstance(payload, dict) and payload.get("error"):
            message = _redact(payload["error"])
            return _envelope(
                q, gl,
                request_count=request_count,
                successful_requests=1,
                error=f"scrape.do maps search failed: {message}",
                error_category=categorize_http_error(None, message),
            )

        local_results = payload.get("local_results") if isinstance(payload, dict) else None
        results = local_results if isinstance(local_results, list) else []
        # Zero results is a legitimate not-found, not an error — but a 200 IS billed, so
        # an empty one is money spent for nothing and worth counting separately (it is
        # the case to raise with scrape.do for a credit refund). Distinct from the 502
        # "no results" path above, which costs nothing.
        return _envelope(
            q, gl,
            request_count=request_count,
            successful_requests=1,
            results=results,
            billed_empty=not results,
        )

    # Unreachable: the loop either returns or exhausts into the error path above.
    return _envelope(q, gl, request_count=request_count,
                     error="scrape.do maps search failed", error_category="internal")
