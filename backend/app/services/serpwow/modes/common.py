# backend/app/services/serpwow/modes/common.py
"""Shared per-mode sub-clients (scrape.do search + gmaps standalone wrappers)."""
from __future__ import annotations

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

async def run_scrapedo_search_for_firmographics(
    official_website: str, country: Optional[str] = None,
) -> dict[str, Any]:
    """One scrape.do Google Search (+ deferred AI-Overview fetch) for a known website.

    Never raises: every failure comes back inside the envelope so the executor can
    classify the row.

    The query is still built from the DOMAIN, not the full URL — see ``_domain_from_url``.
    That is a known limitation: ``https://grupoltn.com/acerolatina`` asks about
    ``grupoltn.com``, so a sub-brand path enriches its parent group instead.
    """
    domain = _domain_from_url(official_website)
    if not domain:
        return {
            "provider": "scrapedo",
            "used": False,
            "domain": None,
            "query": None,
            "request_count": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "credits": 0,
            "ai_overview": None,
            "raw_response": None,
            "error": "Could not extract domain from official website URL",
            "error_category": "input",
        }

    try:
        from app.services.serpwow import scrapedo_search_client as search_client
    except Exception as exc:
        return {
            "provider": "scrapedo",
            "used": False,
            "domain": domain,
            "query": None,
            "request_count": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "credits": 0,
            "ai_overview": None,
            "raw_response": None,
            "error": f"Failed to import scrapedo_search_client.py: {str(exc)}",
            "error_category": "internal",
        }

    query = build_firmographics_query(domain)
    envelope = await search_client.search_with_ai_overview(
        query, gl=_country_to_gl(country))
    # `domain` is not in the client's envelope (it only knows the query string), but the
    # executor and the stored row both report it, so add it here.
    return {**envelope, "domain": domain}


def build_firmographics_query(domain: str) -> str:
    """The one question this pipeline asks Google.

    Kept word-for-word across the 2026-08-19 provider change so runs either side of it are
    comparable: a different fill rate is then the provider's doing, not the query's.
    """
    return (f"What is the address, phone, email, industry, products, "
            f"services of {domain}")


async def run_gmaps_from_module(
    company_name: str,
    country: str,
    input_industry: Optional[str] = None,
    input_full_address: Optional[str] = None,
) -> dict[str, Any]:
    try:
        from app.services.serpwow import scrapedo_maps_client as gmaps_module
    except Exception as exc:
        return {
            "provider": "scrapedo",
            "used": False,
            "query": None,
            "official_website": None,
            "request_count": 0,
            "credits": 0,
            "raw_response": None,
            "error": f"Failed to import scrapedo_maps_client.py: {str(exc)}",
            "error_category": "internal",
        }

    try:
        location_hint = (input_full_address or "").strip() or (country or "").strip()
        query = " ".join(part for part in [company_name or "", location_hint] if part).strip()
        if not query:
            query = (company_name or "").strip()

        gmaps_result = await gmaps_module.process_gmaps_query(
            query, gl=_country_to_gl(country))
        counts = {
            key: max(0, int((gmaps_result or {}).get(key, 0) or 0))
            for key in ("request_count", "successful_requests", "failed_requests", "credits")
        }

        # A provider failure must NOT be reported as "no website found" -- the client
        # returns errors instead of raising, so propagate them here or the row lands as
        # a business not_found and "Rerun failed" can never reach it.
        provider_error = (gmaps_result or {}).get("error")
        if provider_error:
            return {
                "provider": "scrapedo",
                "used": False,
                "query": query,
                "official_website": None,
                **counts,
                "no_results": False,
                "billed_empty": False,
                "raw_response": gmaps_result,
                "error": str(provider_error),
                "error_category": (gmaps_result or {}).get("error_category"),
            }

        gmaps_website = _select_best_gmaps_website(
            gmaps_result,
            company_name=company_name,
            input_full_address=input_full_address,
        )
        if not gmaps_website:
            gmaps_website = gmaps_module.extract_gmaps_website(gmaps_result)
        if is_disallowed_official_url(gmaps_website):
            gmaps_website = None

        return {
            "provider": "scrapedo",
            "used": True,
            "query": query,
            "official_website": gmaps_website,
            **counts,
            # Google has no Maps listing for this company: a not-found, not a failure.
            "no_results": bool((gmaps_result or {}).get("no_results")),
            # Billed (HTTP 200) but zero results — credits spent for nothing.
            "billed_empty": bool((gmaps_result or {}).get("billed_empty")),
            "raw_response": gmaps_result,
            "error": None,
            "error_category": None,
        }
    except Exception as exc:
        return {
            "provider": "scrapedo",
            "used": False,
            "query": None,
            "official_website": None,
            "request_count": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "credits": 0,
            "no_results": False,
            "billed_empty": False,
            "raw_response": None,
            "error": str(exc),
            "error_category": "internal",
        }
