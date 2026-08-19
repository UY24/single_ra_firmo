# backend/app/services/serpwow/serpwow_client.py
"""SerpWow HTTP client + official-website / candidate extraction from responses."""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.services.common.env import get_float_env as _get_float_env
from app.services.serpwow.geo import _country_to_gl
from app.services.serpwow.outcomes import categorize_http_error
from app.services.serpwow.url_utils import is_disallowed_official_url

SERPWOW_API_URL = "https://api.serpwow.com/live/search"

# Set by engine.startup_event() (API and worker both go through it); when set,
# caps concurrent SerpWow HTTP fetches. None means no throttling.
search_fetch_semaphore: Optional[asyncio.Semaphore] = None


def sanitize_serpwow_error_text(value: Any) -> str:
    safe = re.sub(
        r"([?&]api_key=)[^&'\"\s]+", r"\1[REDACTED]", str(value or ""))
    match = re.search(r"Server error '(\d+) ([^']+)'", safe)
    if match:
        return f"SerpWow failed (HTTP {match.group(1)}): {match.group(2)}."
    return safe


def _safe_serpwow_error(exc: Exception, response: Any = None) -> str:
    status = getattr(response, "status_code", None)
    if status is not None:
        payload: Any = None
        try:
            payload = response.json()
        except Exception:
            pass
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error") or "").strip()
            retry_after = payload.get("retry_after") or payload.get("retry after")
            if message:
                error = f"SerpWow failed (HTTP {status}): {message.rstrip('.')}."
                if retry_after:
                    error += f" Retry after {retry_after} seconds."
                return error
        return f"SerpWow failed (HTTP {status})."
    return sanitize_serpwow_error_text(exc) or type(exc).__name__

def _extract_official_website_from_serpwow(data: dict[str, Any]) -> Optional[str]:
    knowledge_graph = data.get("knowledge_graph")
    if isinstance(knowledge_graph, dict):
        kg_url = (knowledge_graph.get("website") or "").strip()
        if kg_url and not is_disallowed_official_url(kg_url):
            return kg_url

    answer_box = data.get("answer_box")
    if isinstance(answer_box, dict):
        for key in ("link", "url"):
            ans_url = (answer_box.get(key) or "").strip()
            if ans_url and not is_disallowed_official_url(ans_url):
                return ans_url

    ai_overview = data.get("ai_overview")
    if isinstance(ai_overview, dict):
        for source in ai_overview.get("ai_overview_sources", []) or []:
            if not isinstance(source, dict):
                continue
            src_url = (source.get("source_url") or "").strip()
            if src_url and not is_disallowed_official_url(src_url):
                return src_url

    for result in data.get("organic_results", []) or []:
        if not isinstance(result, dict):
            continue
        if _is_listing_or_profile_result(result):
            continue
        link = (result.get("link") or result.get("url") or "").strip()
        if link and not is_disallowed_official_url(link):
            return link

    return None


def _serpwow_ai_overview_is_ambiguous(data: dict[str, Any]) -> bool:
    ai_overview = data.get("ai_overview")
    if not isinstance(ai_overview, dict):
        return False
    contents = ai_overview.get("ai_overview_contents")
    if not isinstance(contents, list):
        return False
    joined = " ".join(
        (item.get("text") or "").strip().lower()
        for item in contents
        if isinstance(item, dict)
    )
    markers = (
        "multiple entities",
        "multiple companies",
        "similar entities",
        "similar names",
        "recommended to verify",
        "verify the specific industry",
        "no single, verified url",
        "no dedicated",
        "no single verified",
        "no dedicated top-level domain",
    )
    return any(marker in joined for marker in markers)


def _is_listing_or_profile_result(result: dict[str, Any]) -> bool:
    link = (result.get("link") or result.get("url") or "").strip().lower()
    title = (result.get("title") or "").strip().lower()
    snippet = (result.get("snippet") or "").strip().lower()
    displayed_link = (result.get("displayed_link") or "").strip().lower()
    combined_text = f"{title} {snippet} {displayed_link}"
    listing_terms = (
        "overview",
        "company profile",
        "companies",
        "import export data",
        "shipment",
        "trade data",
        "supplier",
        "member",
        "directory",
        "market insights",
        "annual report",
        "voter list",
        "election board",
        "knowledge center",
    )
    if any(term in combined_text for term in listing_terms):
        return True

    if not link:
        return False
    parsed = urlparse(link)
    path = (parsed.path or "").lower()
    profile_path_markers = (
        "/companies/",
        "/company/",
        "/profile/",
        "/supplier/",
        "/member/",
        "/directory/",
        "/organization/",
    )
    return any(marker in path for marker in profile_path_markers)


def _extract_official_website_candidates_from_serpwow(data: dict[str, Any]) -> list[str]:
    candidates: list[str] = []

    knowledge_graph = data.get("knowledge_graph")
    if isinstance(knowledge_graph, dict):
        kg_url = (knowledge_graph.get("website") or "").strip()
        if kg_url and not is_disallowed_official_url(kg_url):
            candidates.append(kg_url)

    answer_box = data.get("answer_box")
    if isinstance(answer_box, dict):
        for key in ("link", "url"):
            ans_url = (answer_box.get(key) or "").strip()
            if ans_url and not is_disallowed_official_url(ans_url):
                candidates.append(ans_url)

    ai_overview = data.get("ai_overview")
    if isinstance(ai_overview, dict):
        for source in ai_overview.get("ai_overview_sources", []) or []:
            if not isinstance(source, dict):
                continue
            src_url = (source.get("source_url") or "").strip()
            if src_url and not is_disallowed_official_url(src_url):
                candidates.append(src_url)

    for result in data.get("organic_results", []) or []:
        if not isinstance(result, dict):
            continue
        if _is_listing_or_profile_result(result):
            continue
        link = (result.get("link") or result.get("url") or "").strip()
        if link and not is_disallowed_official_url(link):
            candidates.append(link)

    seen: set[str] = set()
    unique_candidates: list[str] = []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            unique_candidates.append(url)
    return unique_candidates


def extract_ai_overview_text(raw_response: Any) -> str:
    """Flatten a SerpWow AI-overview block to plain text (empty if none present).

    Used both to build AI-overview evidence and to detect "empty 200" phases
    (no overview text + no candidates) — see reporting.empty_response_breakdown.
    """
    if not isinstance(raw_response, dict):
        return ""
    overview = raw_response.get("ai_overview")
    if not isinstance(overview, dict):
        return ""
    contents = overview.get("ai_overview_contents")
    if not isinstance(contents, list):
        return ""
    lines: list[str] = []

    def _append(item: Any, prefix: str = "") -> None:
        if not isinstance(item, dict):
            return
        text = item.get("text") or item.get("snippet")
        if isinstance(text, str) and text.strip():
            clean = " ".join(text.split())
            lines.append(f"{prefix} {clean}" if prefix else clean)
        nested = item.get("list")
        if isinstance(nested, list):
            for index, child in enumerate(nested, start=1):
                _append(child, f"{prefix}{index}.")

    for item in contents:
        _append(item)
    return "\n".join(lines)


async def run_serpwow_search(
    query: str,
    country: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    api_key = os.getenv("SERPWOW_API_KEY", "").strip()
    if not api_key:
        return {
            "provider": "serpwow",
            "used": False,
            "query": query,
            "official_website": None,
            "candidates": [],
            "status_code": None,
            "search_url": None,
            "raw_response": None,
            "error": "SERPWOW_API_KEY is not configured",
            "error_category": "auth",
        }

    params = {
        "api_key": api_key,
        "q": query,
        "hl": "en",
        "engine": "google",
        "include_ai_overview": "true",
        "gl": _country_to_gl(country),
    }
    timeout_sec = _get_float_env("SERPWOW_TIMEOUT_SEC", 45.0)

    if client is None:
        async with httpx.AsyncClient(timeout=timeout_sec) as owned_client:
            return await run_serpwow_search(query, country=country, client=owned_client)

    for attempt in range(3):
        response = None
        try:
            if search_fetch_semaphore is not None:
                async with search_fetch_semaphore:
                    response = await client.get(SERPWOW_API_URL, params=params)
            else:
                response = await client.get(SERPWOW_API_URL, params=params)
            response.raise_for_status()
            data = response.json()
            break
        except Exception as exc:
            status = getattr(response, "status_code", None)
            retryable = (
                isinstance(exc, httpx.TransportError)
                or status == 429
                or (status is not None and 500 <= status <= 599)
            )
            if retryable and attempt < 2:
                await asyncio.sleep(2 ** attempt)
                continue
            return {
                "provider": "serpwow",
                "used": False,
                "query": query,
                "official_website": None,
                "candidates": [],
                "status_code": status,
                "search_url": None,
                "raw_response": None,
                "error": _safe_serpwow_error(exc, response),
                "error_category": categorize_http_error(
                    status, f"{type(exc).__name__}: {exc}"
                ),
            }

    request_info = data.get("request_info", {}) if isinstance(data, dict) else {}
    serpwow_raw = data if isinstance(data, dict) else {}
    official_website = _extract_official_website_from_serpwow(serpwow_raw)
    ambiguity_detected = _serpwow_ai_overview_is_ambiguous(serpwow_raw)
    if ambiguity_detected:
        official_website = None
    candidates = _extract_official_website_candidates_from_serpwow(serpwow_raw)
    return {
        "provider": "serpwow",
        "used": True,
        "query": query,
        "official_website": official_website,
        "candidates": candidates,
        "status_code": response.status_code,
        "search_url": request_info.get("search_url") if isinstance(request_info, dict) else None,
        "raw_response": data,
        "error": "Ambiguous entity in AI overview; continuing search." if ambiguity_detected else None,
        "error_category": None,
    }
