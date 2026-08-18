# backend/app/services/serpwow/modes/gmaps.py
"""gmaps mode executor (scrape.do Google Maps search).

One scrape.do request per row: ``local_results[]`` already carries website/title/
address/phone/rating/reviews/type inline, which is everything this pipeline reads.
Billing is credits, not USD, so this executor emits no ``serpwow_*`` cost keys.
See ``scrapedo_maps_client``.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

from app.models.entities import (
    ADDRESS_ALIASES,
    COMPANY_ALIASES,
    COUNTRY_ALIASES,
    FIRM_ID_ALIASES,
    INDUSTRY_ALIASES,
)
from app.services.serpwow.schemas import CrawlResponse
from app.services.serpwow.constants import (
    PIPELINE_GMAPS,
)
from app.services.serpwow import outcomes as _outcomes
from app.services.serpwow.gmaps_scoring import (
    _gmaps_confidence_block,
    _score_gmaps_candidates,
)
from app.services.serpwow.url_utils import dedupe_candidate_urls
from app.services.serpwow.modes.common import (
    run_gmaps_from_module,
)

def row_fields(row: dict[str, Any]) -> dict[str, Any]:
    """The five inputs a gmaps row needs, out of an arbitrary canonical CSV row.

    Header matching reuses models.entities' alias SETS — the same ones parse_entities_csv
    validates the upload against — so a header accepted at upload can never be one this
    fails to find. The row index is the CSV position (0-based, injected by
    s3_run_store.iter_input_rows), NOT a sequence number that skips blank rows: it is the
    row's identity in raw/ and rows/, so it has to survive a row with no company name.
    """
    lowered = {re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_"): v
               for k, v in row.items() if isinstance(k, str)}

    def pick(aliases: set[str]) -> str:
        for key, value in lowered.items():
            if key in aliases and str(value or "").strip():
                return str(value).strip()
        return ""

    return {
        "row_index": row.get("row_index"),
        "company_name": pick(COMPANY_ALIASES),
        "country": pick(COUNTRY_ALIASES),
        "firm_id": pick(FIRM_ID_ALIASES),
        "industry": pick(INDUSTRY_ALIASES),
        "full_address": pick(ADDRESS_ALIASES),
    }


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
    # scrape.do names these `types` (list) / `type` (str).
    categories = (
        first_place.get("types")
        or [c for c in (first_place.get("type"),) if c]
    )

    summary = "Google Maps details lookup successfully resolved." if gmaps_context.get("used") else "Google Maps lookup failed."
    if gmaps_context.get("error"):
        summary = f"Google Maps error: {gmaps_context.get('error')}"

    gmaps_requests_used = int(gmaps_context.get("request_count", 0) or 0)
    gmaps_requests_ok = int(gmaps_context.get("successful_requests", 0) or 0)
    gmaps_requests_failed = int(gmaps_context.get("failed_requests", 0) or 0)
    gmaps_credits_used = int(gmaps_context.get("credits", 0) or 0)
    gmaps_no_results = bool(gmaps_context.get("no_results"))
    # Billed (HTTP 200) but zero results: credits spent for no data. Tracked separately
    # from no_results (which is free) because it's the scrape.do refund-claim case.
    gmaps_billed_empty = bool(gmaps_context.get("billed_empty"))
    if gmaps_no_results:
        summary = "No Google Maps listing exists for this company."

    # Split this row's FAILED attempts by what the row ultimately became, so a 502 that
    # recovered on retry never shows up as an error. The three buckets are mutually
    # exclusive and sum to gmaps_requests_failed:
    #   recovered  -> the row succeeded in the end; those attempts were transient
    #   error      -> the row failed after every retry: the only real errors
    #   (remainder) -> attempts on a row that ended "no Google listing" (free, expected)
    gmaps_recovered_requests = gmaps_requests_failed if gmaps_requests_ok else 0
    gmaps_error_requests = (
        gmaps_requests_failed
        if (not gmaps_requests_ok and not gmaps_no_results) else 0
    )

    # Confidence is HEURISTIC, full stop. The LLM modes (GMAPS_CONFIDENCE_MODE=llm and
    # GMAPS_LLM_BATCH) were removed when this pipeline moved to the S3-only runner: the
    # batch one routed through the shared gsearch chunk engine, whose chunk state lives in
    # state.json — the very thing that capped this pipeline at ~2.7k rows. The scoring
    # below costs nothing and is what every verified run actually used.
    official_website = gmaps_website
    scored = _score_gmaps_candidates(gmaps_result, company_name, input_full_address)
    candidates = dedupe_candidate_urls([e["url"] for e in scored])
    confidence_ctx: dict[str, Any] = {
        "gmaps_confidence": _gmaps_confidence_block(
            gmaps_result, company_name, input_full_address, gmaps_website),
    }

    context: dict[str, Any] = {
        "pipeline": PIPELINE_GMAPS,
        "success": bool(official_website),
        "error": gmaps_context.get("error"),
        # WITHOUT raw_response: that payload is already persisted verbatim as this
        # row's serpwow_response/ artifact (+ S3 mirror), and nothing reads it back
        # from state. Keeping it made each row ~39KB instead of ~2.6KB, and since
        # update_row_state rewrites the WHOLE state file per row, that duplication
        # was the dominant cost of a run (0.85MB state.json at only 100 rows).
        # The in-process consumers (scoring above, the Gemini selector) use the live
        # object, not this copy.
        "gmaps": {k: v for k, v in gmaps_context.items() if k != "raw_response"},
        # One "phase" for the single maps call. This is the structure the row-outcome
        # taxonomy reads (outcomes._phase_stats), so a scrape.do failure classifies as
        # outcome=error/source=scrapedo instead of silently becoming a business
        # not_found. It also gives the row an attempt_log line in found/notFound.csv.
        # `raw_response` is deliberately omitted — it already goes to the S3 artifact,
        # and inlining it here would bloat state.json on every row.
        "formatted_results": [{
            "phase": "gmaps",
            "provider": "scrapedo",
            "query": gmaps_context.get("query"),
            "success": bool(gmaps_context.get("used")),
            "error": gmaps_context.get("error"),
            "error_category": gmaps_context.get("error_category"),
            "error_source": _outcomes.SRC_SCRAPEDO,
            "candidate_count": len(candidates),
            "no_results": gmaps_no_results,
            "billed_empty": gmaps_billed_empty,
        }],
        # scrape.do bills credits, not per-search USD, so there are no serpwow_* keys
        # here — and their ABSENCE is load-bearing: build_summary routes on the
        # scrapedo_* keys and would otherwise price this row's single call at
        # SERPWOW_USD_PER_SEARCH. gemini/total stay as zeros so the
        # report keeps one shape across pipelines.
        "cost_breakdown": {
            # requests == successful + failed, so a run reconciles as
            # "N calls = X succeeded + Y failed"; only the successful ones are billed.
            "scrapedo_requests": gmaps_requests_used,
            "scrapedo_successful_requests": gmaps_requests_ok,
            "scrapedo_failed_requests": gmaps_requests_failed,
            "scrapedo_recovered_requests": gmaps_recovered_requests,
            "scrapedo_error_requests": gmaps_error_requests,
            "scrapedo_no_results": 1 if gmaps_no_results else 0,
            "scrapedo_billed_empty": 1 if gmaps_billed_empty else 0,
            "scrapedo_credits": gmaps_credits_used,
            "gemini_cost_usd": 0.0,
            "total_cost_usd": 0.0,
        },
    }
    context.update(confidence_ctx)
    if gmaps_no_results:
        # Surfaces in found/notFound.csv + the UI instead of the generic not-found text.
        # classify_finalized_row returns not_found for any ctx row_error, sentinel or not.
        context["row_error"] = "No Google Maps listing exists for this company."

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
        # massive_proxy_cost_usd / serpwow_cost_usd left unset (None): neither provider
        # is involved in a gmaps row, so "not applicable" beats a meaningless $0.00.
        # They stay on the shared CrawlResponse for gsearch/firmographics.
        gemini_cost_usd=0.0,
        total_cost_usd=0.0,
        context=context,
    )

    # The row's raw provider payload (scrape.do). The caller still stores it under the
    # shared serpwow_response/ S3 prefix — that layout is cross-pipeline and documented,
    # so renaming it is part of the deferred package-wide rename, not this change.
    raw_json = json.dumps(gmaps_result, ensure_ascii=True, indent=2)
    return response, raw_json
