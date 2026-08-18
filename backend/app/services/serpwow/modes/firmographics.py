# backend/app/services/serpwow/modes/firmographics.py
"""firmographics mode executor (enrich a known official website)."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from app.services.serpwow.schemas import CrawlResponse

from app.services.serpwow.address import (
    _is_address_aligned,
)
from app.services.serpwow.constants import (
    PIPELINE_FIRMOGRAPHICS,
)
from app.services.serpwow.cost import (
    calculate_gemini_cost_usd,
    calculate_serpwow_cost_usd,
)
from app.services.serpwow.gemini_llm import (
    standardize_serpwow_ai_overview_with_gemini,
)
from app.services.serpwow.url_utils import (
    _normalize_website_input,
    is_disallowed_official_url,
)
from app.services.serpwow.modes.common import (
    run_serpwow_from_codetails,
)

async def execute_firmographic_extraction(
    official_website: str,
    company_name: Optional[str],
    country: Optional[str],
    firm_id: Optional[str] = None,
    input_industry: Optional[str] = None,
    input_full_address: Optional[str] = None,
) -> tuple[CrawlResponse, str]:
    normalized_official = _normalize_website_input(official_website)
    # Empty when the input CSV had no company column — see csv_input: no invented names.
    clean_company_name = (company_name or "").strip()
    clean_country = (country or "").strip()
    clean_full_address = (input_full_address or "").strip() or None
    summary = "Firmographic extraction completed from provided official website."

    if not normalized_official or is_disallowed_official_url(normalized_official):
        response = CrawlResponse(
            company_name=clean_company_name,
            country=clean_country,
            firm_id=firm_id,
            input_industry=input_industry,
            input_full_address=input_full_address,
            official_website=None,
            summary="Invalid or unsupported official website for firmographic extraction.",
            address=None,
            phone=None,
            email=None,
            industry=None,
            products=[],
            services=[],
            massive_proxy_cost_usd=0.0,
            serpwow_cost_usd=0.0,
            gemini_cost_usd=0.0,
            total_cost_usd=0.0,
            context={
                "pipeline": PIPELINE_FIRMOGRAPHICS,
                "serpwow": {
                    "provider": "serpwow",
                    "used": False,
                    "domain": None,
                    "query": None,
                    "request_count": 0,
                    "ai_overview": None,
                    "raw_response": None,
                    "error": "Invalid official website input.",
                },
                "serpwow_mapping_ai": {
                    "provider": "google-gemini",
                    "model": None,
                    "used": False,
                    "error": "Skipped because official_website was invalid.",
                    "usage": {},
                    "raw": None,
                },
                "cost_breakdown": {
                    "massive_proxy_cost_usd": 0.0,
                    "serpwow_cost_usd": 0.0,
                    "gemini_cost_usd": 0.0,
                    "total_cost_usd": 0.0,
                    "serpwow_request_count": 0,
                },
            },
        )
        return response, ""

    serpwow_context = await run_serpwow_from_codetails(normalized_official, country=clean_country)
    serpwow_raw_json = (
        json.dumps(serpwow_context.get("raw_response"), ensure_ascii=True, indent=2)
        if serpwow_context.get("raw_response") is not None
        else ""
    )

    mapped_columns = {
        "address": None,
        "phone": None,
        "email": None,
        "industry": None,
        "products": [],
        "services": [],
    }
    serpwow_mapping_ai_context = {
        "provider": "google-gemini",
        "model": None,
        "used": False,
        "error": "Skipped because SerpWow ai_overview was unavailable.",
        "usage": {},
        "raw": None,
    }

    if isinstance(serpwow_context.get("ai_overview"), dict):
        mapped_output, mapped_error, mapped_model, mapped_usage = (
            await asyncio.to_thread(
                standardize_serpwow_ai_overview_with_gemini,
                clean_company_name,
                clean_country,
                normalized_official,
                serpwow_context.get("ai_overview") or {},
            )
        )
        if isinstance(mapped_output, dict):
            mapped_columns = {
                "address": mapped_output.get("address"),
                "phone": mapped_output.get("phone"),
                "email": mapped_output.get("email"),
                "industry": mapped_output.get("industry"),
                "products": mapped_output.get("products") or [],
                "services": mapped_output.get("services") or [],
            }
        serpwow_mapping_ai_context = {
            "provider": "google-gemini",
            "model": mapped_model,
            "used": mapped_output is not None,
            "error": mapped_error,
            "usage": mapped_usage or {},
            "raw": mapped_output,
        }

    if clean_full_address:
        mapped_address_value = mapped_columns.get("address")
        if mapped_address_value and not _is_address_aligned(clean_full_address, mapped_address_value):
            summary = (
                "Firmographics extracted, but mapped address is not aligned with provided input address."
            )

    serpwow_mapping_ai_cost_usd = calculate_gemini_cost_usd(
        serpwow_mapping_ai_context.get("usage")
        if isinstance(serpwow_mapping_ai_context, dict)
        else None
    )
    serpwow_request_count = int(serpwow_context.get("request_count", 0) or 0)
    serpwow_cost_usd = calculate_serpwow_cost_usd(serpwow_request_count)
    gemini_cost_usd = round(serpwow_mapping_ai_cost_usd, 8)
    total_cost_usd = round(serpwow_cost_usd + gemini_cost_usd, 8)

    response = CrawlResponse(
        company_name=clean_company_name,
        country=clean_country,
        firm_id=firm_id,
        input_industry=input_industry,
        input_full_address=input_full_address,
        official_website=normalized_official,
        summary=summary,
        address=mapped_columns.get("address"),
        phone=mapped_columns.get("phone"),
        email=mapped_columns.get("email"),
        industry=mapped_columns.get("industry"),
        products=mapped_columns.get("products") or [],
        services=mapped_columns.get("services") or [],
        massive_proxy_cost_usd=0.0,
        serpwow_cost_usd=serpwow_cost_usd,
        gemini_cost_usd=gemini_cost_usd,
        total_cost_usd=total_cost_usd,
        context={
            "pipeline": PIPELINE_FIRMOGRAPHICS,
            "serpwow": serpwow_context,
            "serpwow_mapping_ai": serpwow_mapping_ai_context,
            "cost_breakdown": {
                "massive_proxy_cost_usd": 0.0,
                "serpwow_cost_usd": serpwow_cost_usd,
                "gemini_cost_usd": gemini_cost_usd,
                "total_cost_usd": total_cost_usd,
                "serpwow_request_count": serpwow_request_count,
            },
        },
    )
    return response, serpwow_raw_json
