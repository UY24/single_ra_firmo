# backend/app/services/serpwow/serpwow_client.py
"""SerpWow HTTP client + official-website / candidate extraction from responses."""
from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.services.common.env import get_float_env as _get_float_env
from app.services.serpwow.geo import _country_to_gl
from app.services.serpwow.url_utils import is_disallowed_official_url

SERPWOW_API_URL = "https://api.serpwow.com/live/search"

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

    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout_sec) as owned_client:
                return await run_serpwow_search(query, country=country, client=owned_client)
        if search_fetch_semaphore is not None:
            async with search_fetch_semaphore:
                response = await client.get(SERPWOW_API_URL, params=params)
        else:
            response = await client.get(SERPWOW_API_URL, params=params)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return {
            "provider": "serpwow",
            "used": False,
            "query": query,
            "official_website": None,
            "candidates": [],
            "status_code": None,
            "search_url": None,
            "raw_response": None,
            "error": str(exc),
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
    }
