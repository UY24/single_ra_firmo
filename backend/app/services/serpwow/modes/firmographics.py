# backend/app/services/serpwow/modes/firmographics.py
"""firmographics mode executor (enrich a known official website).

Provider: scrape.do Google Search (``scrapedo_search_client``). Billing is CREDITS ONLY —
10 per search HTTP 200, 5 per deferred AI-Overview HTTP 200 — so the row carries
``scrapedo_*`` cost keys and leaves every per-provider USD field unset, i.e. ``None``:
"not applicable" rather than a misleading $0.00, the convention gmaps set. The only USD on
the row is the Gemini normalisation call.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from app.services.serpwow.schemas import CrawlResponse

from app.services.serpwow.address import (
    _is_address_aligned,
)
from app.services.common import llm_batch
from app.services.serpwow.constants import (
    PIPELINE_FIRMOGRAPHICS,
)
from app.services.serpwow.cost import (
    calculate_gemini_cost_usd,
)
from app.services.serpwow.gemini_llm import (
    standardize_ai_overview_with_gemini,
)
from app.services.serpwow.url_utils import (
    _normalize_website_input,
    is_disallowed_official_url,
)
from app.services.serpwow.modes.common import (
    run_scrapedo_search_for_firmographics,
)

_EMPTY_MAPPED_COLUMNS: dict[str, Any] = {
    "address": None,
    "phone": None,
    "email": None,
    "industry": None,
    "products": [],
    "services": [],
}


def row_fields(row: dict[str, Any]) -> dict[str, Any]:
    """The inputs a firmographics row needs, out of an arbitrary input.csv row.

    Header resolution goes through ``csv_input.firmographics_columns`` -- the SAME alias
    table the upload validates against -- so a header accepted at upload can never be one
    the worker fails to find. The row index is the CSV position injected by
    ``s3_run_store.iter_input_rows``, not a sequence number that skips blanks: it is the
    row's identity in raw/, rows/ and cleaned/, so it has to survive a row with no website.
    """
    from app.services.serpwow.csv_input import firmographics_columns

    columns = firmographics_columns(list(row.keys()))

    def pick(canonical: str) -> str:
        key = columns.get(canonical)
        return str(row.get(key) or "").strip() if key else ""

    return {
        "row_index": row.get("row_index"),
        "official_website": _normalize_website_input(pick("website_url")),
        "company_name": pick("company_name"),
        "country": pick("country"),
        "firm_id": pick("firm_id"),
        "industry": pick("industry"),
        "full_address": pick("full_address"),
    }


def _cost_breakdown(scrapedo: dict[str, Any], gemini_cost_usd: float) -> dict[str, Any]:
    """Credit accounting for one row, in the shape the run summary routes on: the presence
    of ``scrapedo_requests``/``scrapedo_credits`` is what makes ``build_summary`` account
    for this row in credits instead of per-request USD.

    The per-endpoint counts are kept, not just the totals, because the two endpoints cost
    different amounts: without them a bill of 1150 credits over 100 rows is unexplainable.
    """
    search_requests = int(scrapedo.get("search_requests") or 0)
    search_ok = int(scrapedo.get("search_successful") or 0)
    aio_requests = int(scrapedo.get("ai_overview_requests") or 0)
    aio_ok = int(scrapedo.get("ai_overview_successful") or 0)
    requests = int(scrapedo.get("request_count") or 0)
    successful = int(scrapedo.get("successful_requests") or 0)
    errored = bool(scrapedo.get("error"))
    return {
        "scrapedo_requests": requests,
        "scrapedo_successful_requests": successful,
        "scrapedo_failed_requests": max(0, requests - successful),
        "scrapedo_credits": int(scrapedo.get("credits") or 0),
        # Per-endpoint split, since 10 credits and 5 credits are not interchangeable.
        "scrapedo_search_requests": search_requests,
        "scrapedo_search_successful": search_ok,
        "scrapedo_ai_overview_requests": aio_requests,
        "scrapedo_ai_overview_successful": aio_ok,
        # Rows where Google deferred the overview, i.e. the 5-credit follow-up was needed.
        "scrapedo_ai_overview_deferred": 1 if scrapedo.get("deferred") else 0,
        # Billed a search but got no usable overview: credits spent for nothing, the
        # refund-claim case. Same key gmaps uses so the UI reads one field.
        "scrapedo_billed_empty": 1 if scrapedo.get("billed_no_overview") else 0,
        # A row that failed after every retry. Free when it never saw a 200; billed when
        # the 200 itself carried an error body.
        "scrapedo_error_requests": max(0, requests - successful) if errored else 0,
        "scrapedo_billed_errors": 1 if (errored and successful) else 0,
        "gemini_cost_usd": gemini_cost_usd,
        # Credits are not dollars. The only USD this pipeline spends is the LLM call.
        "total_cost_usd": gemini_cost_usd,
    }


async def execute_firmographic_extraction(
    official_website: str,
    company_name: Optional[str],
    country: Optional[str],
    firm_id: Optional[str] = None,
    input_industry: Optional[str] = None,
    input_full_address: Optional[str] = None,
) -> tuple[CrawlResponse, str]:
    normalized_official = _normalize_website_input(official_website)
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
            **_EMPTY_MAPPED_COLUMNS,
            # Per-provider USD fields are left at their None default: this pipeline bills
            # in credits, and no call was made on this row anyway.
            gemini_cost_usd=0.0,
            total_cost_usd=0.0,
            context={
                "pipeline": PIPELINE_FIRMOGRAPHICS,
                "row_error": "Invalid official website input.",
                "scrapedo": {
                    "provider": "scrapedo",
                    "used": False,
                    "query": None,
                    "error": "Invalid official website input.",
                    "error_category": "input",
                },
                "mapping_ai": {
                    "provider": "google-gemini",
                    "model": None,
                    "used": False,
                    "error": "Skipped because official_website was invalid.",
                    "usage": {},
                    "raw": None,
                },
                "cost_breakdown": _cost_breakdown({}, 0.0),
            },
        )
        return response, ""

    # The two phases are timed separately: this pipeline interleaves them per row (there
    # is no run-level scrape-then-clean split), so "where did the time go" can only be
    # answered per row and then averaged.
    _started = time.perf_counter()
    scrapedo_context = await run_scrapedo_search_for_firmographics(
        normalized_official, country=clean_country)
    search_seconds = round(time.perf_counter() - _started, 3)
    # The row's durable raw artifact is the provider's WHOLE SERP, verbatim: the overview
    # we use plus the knowledge_graph / organic_results we do not yet, so a stored row can
    # be re-judged without re-buying the call.
    raw_json = (
        json.dumps(scrapedo_context.get("raw_response"), ensure_ascii=True, indent=2)
        if scrapedo_context.get("raw_response") is not None
        else ""
    )

    mapped_columns = dict(_EMPTY_MAPPED_COLUMNS)
    mapping_ai_context: dict[str, Any] = {
        "provider": "google-gemini",
        "model": None,
        "used": False,
        "error": "Skipped because no AI overview was available.",
        "usage": {},
        "raw": None,
    }

    llm_seconds = 0.0
    ai_overview = scrapedo_context.get("ai_overview")
    # Batch mode: the normalisation is done later, in one Gemini Batch job over the whole
    # upload (engine.run_gemini_batch_for_upload). Skipping the inline call here is the
    # whole point -- doing both would pay for every row twice.
    batch_mode = llm_batch.batch_enabled(PIPELINE_FIRMOGRAPHICS)
    if batch_mode:
        mapping_ai_context = {
            "provider": "google-gemini",
            "model": None,
            "used": False,
            "error": None,
            "usage": {},
            "raw": None,
            "deferred_to_batch": True,
        }
    if isinstance(ai_overview, dict) and ai_overview and not batch_mode:
        _started = time.perf_counter()
        mapped_output, mapped_error, mapped_model, mapped_usage = (
            await asyncio.to_thread(
                standardize_ai_overview_with_gemini,
                clean_company_name,
                clean_country,
                normalized_official,
                ai_overview,
            )
        )
        llm_seconds = round(time.perf_counter() - _started, 3)
        if isinstance(mapped_output, dict):
            mapped_columns = {key: mapped_output.get(key) for key in
                              ("address", "phone", "email", "industry")}
            mapped_columns["products"] = mapped_output.get("products") or []
            mapped_columns["services"] = mapped_output.get("services") or []
        mapping_ai_context = {
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

    gemini_cost_usd = round(
        calculate_gemini_cost_usd(mapping_ai_context.get("usage")), 8)
    cost_breakdown = _cost_breakdown(scrapedo_context, gemini_cost_usd)

    # A provider failure must be visible as an ERROR, not reported as "no firmographics
    # found": the executor writes the one-entry formatted_results that
    # outcomes._phase_stats reads, tagged with scrape.do as the source, so a 429/auth
    # failure lands as outcome=error and is reachable by "Rerun failed".
    context: dict[str, Any] = {
        "pipeline": PIPELINE_FIRMOGRAPHICS,
        "scrapedo": scrapedo_context,
        "mapping_ai": mapping_ai_context,
        "cost_breakdown": cost_breakdown,
        # The worker merges total_seconds into this dict; these two say how the row's time
        # divided between the provider and the LLM.
        "timing": {"search_seconds": search_seconds, "llm_seconds": llm_seconds},
    }
    provider_error = scrapedo_context.get("error")
    if provider_error:
        summary = "Firmographic extraction failed: the search provider returned an error."
        context["formatted_results"] = [{
            "phase": "scrapedo_search",
            "success": False,
            "error": str(provider_error),
            "error_source": "scrapedo",
            "error_category": scrapedo_context.get("error_category") or "internal",
        }]
    elif batch_mode and isinstance(ai_overview, dict) and ai_overview:
        # Terminal for the row task, but the batch has the last word on the six fields.
        summary = "AI overview captured; awaiting Gemini batch normalisation."
    elif not isinstance(ai_overview, dict) or not ai_overview:
        # Billed, answered, but Google had no AI Overview (or the deferred fetch failed).
        # A business not-found for enrichment purposes, NOT an error.
        summary = "No AI overview was available for this website; no firmographics extracted."
        context["row_error"] = (
            str(scrapedo_context.get("ai_overview_error"))
            if scrapedo_context.get("ai_overview_error")
            else "Google returned no AI overview for this website."
        )

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
        # Credits, not dollars: the per-provider USD fields stay None ("not applicable"),
        # and the row's total USD is the LLM call alone.
        gemini_cost_usd=gemini_cost_usd,
        total_cost_usd=gemini_cost_usd,
        context=context,
    )
    return response, raw_json
