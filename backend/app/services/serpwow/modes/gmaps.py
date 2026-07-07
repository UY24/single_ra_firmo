# backend/app/services/serpwow/modes/gmaps.py
"""gmaps mode executor (SerpWow Places)."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

from app.services.serpwow.schemas import CrawlResponse
from app.services.common.env import (
    get_bool_env as _get_bool_env,
    get_int_env as _get_int_env,
    get_float_env as _get_float_env,
)

from app.services.serpwow.constants import (
    PIPELINE_GMAPS,
)
from app.services.serpwow.cost import (
    calculate_gemini_cost_usd,
    calculate_serpwow_cost_usd,
)
from app.services.serpwow.gemini_llm import (
    choose_final_website_with_gemini,
)
from app.services.serpwow.gmaps_scoring import (
    _gmaps_confidence_block,
    _score_gmaps_candidates,
)
from app.services.serpwow.url_utils import (
    dedupe_candidate_urls,
    is_disallowed_official_url,
)
from app.services.serpwow.modes.common import (
    run_gmaps_from_module,
)

async def execute_gmaps_lookup(
    company_name: str,
    country: str,
    firm_id: Optional[str] = None,
    input_industry: Optional[str] = None,
    input_full_address: Optional[str] = None,
    debug_upload_id: Optional[str] = None,
    debug_row_index: Optional[int] = None,
) -> tuple[CrawlResponse, str]:
    started_monotonic = asyncio.get_event_loop().time()
    
    # Run only Google Maps search using run_gmaps_from_module
    gmaps_context = await run_gmaps_from_module(
        company_name=company_name,
        country=country,
        input_industry=input_industry,
        input_full_address=input_full_address,
    )
    
    gmaps_website = gmaps_context.get("official_website")
    gmaps_result = gmaps_context.get("raw_response") or {}
    
    # Extract details from first result or result if it's a list
    results = gmaps_result.get("results") if isinstance(gmaps_result, dict) else None
    first_place = results[0] if (isinstance(results, list) and len(results) > 0) else gmaps_result
    if not isinstance(first_place, dict):
        first_place = {}
        
    address = first_place.get("address") or first_place.get("formatted_address")
    phone = first_place.get("phone")
    rating = first_place.get("rating")
    reviews = first_place.get("reviews")
    categories = first_place.get("categories") or ([first_place.get("category")] if first_place.get("category") else [])
    
    summary = "Google Maps details lookup successfully resolved." if gmaps_context.get("used") else "Google Maps lookup failed."
    if gmaps_context.get("error"):
        summary = f"Google Maps error: {gmaps_context.get('error')}"
        
    gmaps_requests_used = int(gmaps_context.get("request_count", 0) or 0)
    serpwow_cost_usd = calculate_serpwow_cost_usd(gmaps_requests_used)

    # Confidence: heuristic by default; LLM when GMAPS_CONFIDENCE_MODE=llm. The LLM
    # path reuses gsearch's selector + context keys so serpwow_reporting/build_summary
    # surface confidence/model/tokens/cost with no reporting changes.
    official_website = gmaps_website
    gemini_cost_usd = 0.0
    mode = (os.getenv("GMAPS_CONFIDENCE_MODE", "heuristic") or "heuristic").strip().lower()
    llm_batch = _get_bool_env("GMAPS_LLM_BATCH", False)
    # Candidate set the LLM selects from (also feeds _build_batch_prompt_for_row).
    scored = _score_gmaps_candidates(gmaps_result, company_name, input_full_address)
    candidates = dedupe_candidate_urls([e["url"] for e in scored])
    confidence_ctx: dict[str, Any] = {}

    if mode == "llm" and candidates and not llm_batch:
        final_output, final_error, final_model, final_usage = await asyncio.to_thread(
            choose_final_website_with_gemini,
            company_name, country, input_industry, input_full_address,
            candidates, [], None, gmaps_context,
        )
        if final_output is not None:
            confidence_ctx["candidates"] = candidates
            confidence_ctx["final_url_selection_ai"] = {
                "provider": "google-gemini", "model": final_model,
                "used": True, "error": final_error,
                "usage": final_usage or {}, "raw": final_output,
            }
            gemini_cost_usd = calculate_gemini_cost_usd(final_usage)
            ai_website = final_output.get("official_website")
            # Accept the LLM's validated in-candidate pick; otherwise keep the Python
            # best pick (parity with the gsearch worker).
            if (isinstance(ai_website, str) and ai_website.strip()
                    and not is_disallowed_official_url(ai_website)):
                official_website = ai_website.strip()
        else:
            # LLM error -> heuristic fallback so the row still has a confidence.
            confidence_ctx["candidates"] = candidates
            block = _gmaps_confidence_block(
                gmaps_result, company_name, input_full_address, gmaps_website)
            block["mode"] = f"llm (fallback->heuristic: {final_error})"
            confidence_ctx["gmaps_confidence"] = block
    elif mode == "llm" and llm_batch:
        # Batch mode: the finalization Gemini batch decides. Expose candidates for the
        # batch prompt builder; keep the Python pick + a heuristic placeholder for
        # pre-batch display (serpwow_reporting prefers gemini_batch_ai once it lands).
        confidence_ctx["candidates"] = candidates
        confidence_ctx["gmaps_confidence"] = _gmaps_confidence_block(
            gmaps_result, company_name, input_full_address, gmaps_website)
    else:
        # Heuristic mode (default) — unchanged behavior.
        confidence_ctx["gmaps_confidence"] = _gmaps_confidence_block(
            gmaps_result, company_name, input_full_address, gmaps_website)

    context: dict[str, Any] = {
        "pipeline": PIPELINE_GMAPS,
        "success": bool(official_website),
        "used_proxy": False,
        "blocked": False,
        "error": gmaps_context.get("error"),
        "gmaps": gmaps_context,
        "cost_breakdown": {
            "massive_proxy_cost_usd": 0.0,
            "serpwow_cost_usd": serpwow_cost_usd,
            "gemini_cost_usd": gemini_cost_usd,
            "total_cost_usd": serpwow_cost_usd + gemini_cost_usd,
            "serpwow_request_count": gmaps_requests_used,
        },
    }
    context.update(confidence_ctx)

    # Create CrawlResponse
    response = CrawlResponse(
        company_name=company_name,
        country=country,
        firm_id=firm_id,
        input_industry=input_industry,
        input_full_address=input_full_address,
        official_website=official_website,
        summary=summary,
        address=address,
        phone=phone,
        email=None,
        industry=categories[0] if (categories and len(categories) > 0) else None,
        products=[],
        services=[],
        massive_proxy_cost_usd=0.0,
        serpwow_cost_usd=serpwow_cost_usd,
        gemini_cost_usd=gemini_cost_usd,
        total_cost_usd=serpwow_cost_usd + gemini_cost_usd,
        context=context,
    )

    serpwow_raw_json = json.dumps(gmaps_result, ensure_ascii=True, indent=2)
    return response, serpwow_raw_json
