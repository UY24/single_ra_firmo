# backend/app/services/serpwow/modes/common.py
"""Shared per-mode sub-clients (codetails + gmaps standalone wrappers)."""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from app.services.serpwow.geo import (
    _country_to_gl,
)
from app.services.serpwow.gmaps_scoring import (
    _select_best_gmaps_website,
)
from app.services.serpwow.url_utils import (
    _domain_from_url,
    is_disallowed_official_url,
)

async def run_serpwow_from_codetails(official_website: str, country: Optional[str] = None) -> dict[str, Any]:
    domain = _domain_from_url(official_website)
    if not domain:
        return {
            "provider": "serpwow",
            "used": False,
            "domain": None,
            "query": None,
            "request_count": 0,
            "ai_overview": None,
            "raw_response": None,
            "error": "Could not extract domain from official website URL",
        }

    try:
        from app.services.serpwow import codetails as codetails_module
    except Exception as exc:
        return {
            "provider": "serpwow",
            "used": False,
            "domain": domain,
            "query": None,
            "request_count": 0,
            "ai_overview": None,
            "raw_response": None,
            "error": f"Failed to import codetails.py: {str(exc)}",
        }

    try:
        request_count = 0
        query = (
            codetails_module.build_query(domain)
            if hasattr(codetails_module, "build_query")
            else f"What is the address, phone, email, industry, products, services of {domain}"
        )
        gl = _country_to_gl(country)
        raw_data = await asyncio.to_thread(codetails_module.fetch_serpwow, query, gl)
        request_count += 1
        ai_overview = (
            codetails_module.get_ai_overview(raw_data)
            if hasattr(codetails_module, "get_ai_overview")
            else raw_data.get("ai_overview", {})
        )

        is_placeholder = (
            bool(codetails_module.ai_overview_is_placeholder(ai_overview))
            if hasattr(codetails_module, "ai_overview_is_placeholder")
            else False
        )
        if is_placeholder and hasattr(codetails_module, "build_fallback_query"):
            fallback_query = codetails_module.build_fallback_query(domain)
            fallback_data = await asyncio.to_thread(
                codetails_module.fetch_serpwow, fallback_query, gl
            )
            request_count += 1
            fallback_ai = (
                codetails_module.get_ai_overview(fallback_data)
                if hasattr(codetails_module, "get_ai_overview")
                else fallback_data.get("ai_overview", {})
            )
            if not codetails_module.ai_overview_is_placeholder(fallback_ai):
                raw_data = fallback_data
                ai_overview = fallback_ai
                query = fallback_query

        return {
            "provider": "serpwow",
            "used": True,
            "domain": domain,
            "query": query,
            "request_count": request_count,
            "ai_overview": ai_overview,
            "raw_response": raw_data,
            "error": None,
        }
    except Exception as exc:
        return {
            "provider": "serpwow",
            "used": False,
            "domain": domain,
            "query": None,
            "request_count": 0,
            "ai_overview": None,
            "raw_response": None,
            "error": str(exc),
        }


async def run_gmaps_from_module(
    company_name: str,
    country: str,
    input_industry: Optional[str] = None,
    input_full_address: Optional[str] = None,
) -> dict[str, Any]:
    try:
        from app.services.serpwow import gmaps_client as gmaps_module
    except Exception as exc:
        return {
            "provider": "gmaps",
            "used": False,
            "query": None,
            "official_website": None,
            "request_count": 0,
            "raw_response": None,
            "error": f"Failed to import gmaps_client.py: {str(exc)}",
        }

    try:
        location_hint = (input_full_address or "").strip() or (country or "").strip()
        query = " ".join(part for part in [company_name or "", location_hint] if part).strip()
        if not query:
            query = (company_name or "").strip()

        gmaps_result = await gmaps_module.process_gmaps_query(query, country=_country_to_gl(country))
        gmaps_website = _select_best_gmaps_website(
            gmaps_result,
            company_name=company_name,
            input_full_address=input_full_address,
        )
        if not gmaps_website:
            gmaps_website = (
                gmaps_module.extract_gmaps_website(gmaps_result)
                if hasattr(gmaps_module, "extract_gmaps_website")
                else None
            )
        if is_disallowed_official_url(gmaps_website):
            gmaps_website = None

        request_count = int((gmaps_result or {}).get("request_count", 0) or 0)
        if request_count < 0:
            request_count = 0

        return {
            "provider": "gmaps",
            "used": True,
            "query": query,
            "official_website": gmaps_website,
            "request_count": request_count,
            "raw_response": gmaps_result,
            "error": None,
        }
    except Exception as exc:
        return {
            "provider": "gmaps",
            "used": False,
            "query": None,
            "official_website": None,
            "request_count": 0,
            "raw_response": None,
            "error": str(exc),
        }
