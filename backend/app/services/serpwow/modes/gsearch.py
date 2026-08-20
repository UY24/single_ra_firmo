# backend/app/services/serpwow/modes/gsearch.py
"""gsearch mode executor (SerpWow Google AI Overview)."""
from __future__ import annotations

import asyncio
import httpx
import json
from typing import Any, Optional

from app.services.serpwow.schemas import CrawlResponse
from app.services.common.env import (
    get_bool_env as _get_bool_env,
    get_int_env as _get_int_env,
    get_float_env as _get_float_env,
)

from app.services.serpwow.address import (
    _extract_address_component,
    _marker_variants,
    _normalize_location_token,
)
from app.services.common import llm_batch
from app.services.serpwow.constants import (
    PIPELINE_GSEARCH,
)
from app.services.serpwow.cost import (
    calculate_gemini_cost_usd,
    calculate_serpwow_cost_usd,
)
from app.services.serpwow.gemini_llm import (
    choose_final_website_with_gemini,
)
from app.services.serpwow.outcomes import categorize_http_error
from app.services.serpwow.query_builders import (
    _company_name_variants,
    _extract_phase5_pivots_from_serpwow,
    build_selected_phase_queries,
)
from app.services.serpwow.serpwow_client import (
    extract_ai_overview_text,
    run_serpwow_search,
)
from app.services.serpwow.url_utils import (
    dedupe_candidate_urls,
    is_disallowed_official_url,
)

async def execute_gsearch_lookup_for_worker(
    company_name: str,
    country: str,
    firm_id: Optional[str] = None,
    input_industry: Optional[str] = None,
    input_full_address: Optional[str] = None,
    debug_upload_id: Optional[str] = None,
    debug_row_index: Optional[int] = None,
    phase: str = "all",
) -> tuple[CrawlResponse, str]:
    queries = build_selected_phase_queries(
        company_name=company_name,
        country=country,
        parsed_city_state="",
        full_address=input_full_address or "",
        industry=input_industry or "",
        phase=phase,
    )
    
    if phase == "phase5":
        phase1_queries = build_selected_phase_queries(
            company_name=company_name,
            country=country,
            parsed_city_state="",
            full_address=input_full_address or "",
            industry=input_industry or "",
            phase="phase1",
        )
        target_people = []
        target_trade_names = []
        if phase1_queries:
            quick_res = await run_serpwow_search(phase1_queries[0][1], country=country)
            raw_resp = quick_res.get("raw_response")
            extracted_people, extracted_trade_names = _extract_phase5_pivots_from_serpwow(raw_resp)
            target_people.extend(extracted_people)
            target_trade_names.extend(extracted_trade_names)
            
        clean_company = _normalize_location_token(company_name)
        clean_country = _normalize_location_token(country)
        variants = _company_name_variants(clean_company)
        v_primary = variants[0] if variants else clean_company
        
        plot_raw = _extract_address_component(input_full_address, ("plot",)) if input_full_address else ""
        road_raw = _extract_address_component(input_full_address, ("road", " rd")) if input_full_address else ""
        plot_hyphen, _ = _marker_variants(plot_raw) if plot_raw else ("", "")
        road_hyphen, _ = _marker_variants(road_raw) if road_raw else ("", "")
        
        phase5_attempt_queries = []
        for person in target_people:
            q = f'"{person}" "{v_primary}"'
            phase5_attempt_queries.append(("phase5_person_connection", q))
            
        for trade in target_trade_names:
            if plot_hyphen and road_hyphen:
                q = f'"{trade}" "{plot_hyphen}" "{road_hyphen}"'
                phase5_attempt_queries.append(("phase5_trade_address_connection", q))
            else:
                q = f'"{trade}" "{v_primary}" {clean_country}'
                phase5_attempt_queries.append(("phase5_trade_connection", q))
            
            q_web = f'"{trade}" official website {clean_country}'
            phase5_attempt_queries.append(("phase5_trade_official_website", q_web))
            
        queries = phase5_attempt_queries
        
    timeout_sec = _get_float_env("SERPWOW_TIMEOUT_SEC", 45.0)
    async with httpx.AsyncClient(timeout=timeout_sec) as serpwow_client:
        results = await asyncio.gather(
            *[
                run_serpwow_search(q[1], country=country, client=serpwow_client)
                for q in queries
            ],
            return_exceptions=True,
        )
        
    formatted_results = []
    candidates = []
    seen_candidates = set()
    search_attempts: list[dict[str, Any]] = []
    first_raw: Optional[dict[str, Any]] = None

    billable_requests = 0
    for (label, query), raw_result in zip(queries, results):
        if isinstance(raw_result, Exception):
            raw_result = {
                "provider": "serpwow",
                "used": False,
                "query": query,
                "official_website": None,
                "candidates": [],
                "status_code": None,
                "search_url": None,
                "raw_response": None,
                "error": f"{type(raw_result).__name__}: {str(raw_result)}",
                "error_category": categorize_http_error(
                    None, f"{type(raw_result).__name__}: {raw_result}"),
            }

        if raw_result.get("used"):
            billable_requests += 1
        attempt_cands = raw_result.get("candidates") or []
        phase_candidate_count = 0
        for cand in attempt_cands:
            if cand and not is_disallowed_official_url(cand):
                phase_candidate_count += 1
                if cand not in seen_candidates:
                    seen_candidates.add(cand)
                    candidates.append(cand)

        formatted_results.append({
            "phase": label,
            "query": query,
            "success": bool(raw_result.get("used")),
            "error": raw_result.get("error"),
            "error_category": raw_result.get("error_category"),
            "status_code": raw_result.get("status_code"),
            "search_url": raw_result.get("search_url"),
            # AI-overview presence + usable-candidate count per phase, so the reporting
            # layer can flag "empty 200" phases (no overview + 0 candidates) uniformly
            # with relationship mode. See reporting.empty_response_breakdown.
            "ai_overview_present": bool(extract_ai_overview_text(raw_result.get("raw_response"))),
            "candidate_count": phase_candidate_count,
            # raw_response deliberately NOT stored: it's already persisted as this
            # row's serpwow_response/ artifact, nothing reads it back from state, and
            # inlining it here put one payload PER PHASE (up to 5) into a state file
            # that gets rewritten in full on every row update.
        })

        search_attempts.append({
            "attempt": label,
            "query": query,
            "search_url": raw_result.get("search_url"),
            "status": "official_website_found" if raw_result.get("official_website") else "no_valid_website",
            "status_code": raw_result.get("status_code"),
            "error": raw_result.get("error"),
            "official_website": raw_result.get("official_website"),
        })

        if first_raw is None and isinstance(raw_result.get("raw_response"), dict):
            first_raw = raw_result.get("raw_response")

    serpwow_cost = calculate_serpwow_cost_usd(billable_requests)
    best_candidate = candidates[0] if candidates else None
    official_website = best_candidate
    skip_llm = not candidates

    final_url_selection_ai = {
        "provider": "google-gemini", "model": None, "used": False,
        "error": "Skipped: batch mode, ENABLE_FINAL_URL_GEMINI off, or no candidates.",
        "usage": {}, "raw": None,
    }
    gemini_cost = 0.0
    llm_error_for_row: Optional[str] = None
    batch_mode = llm_batch.batch_enabled(PIPELINE_GSEARCH)
    enable_final = _get_bool_env("ENABLE_FINAL_URL_GEMINI", True)
    if not batch_mode and enable_final and candidates:
        final_output, final_error, final_model, final_usage = await asyncio.to_thread(
            choose_final_website_with_gemini,
            company_name, country, input_industry, input_full_address,
            candidates, search_attempts, first_raw, {},
        )
        # A genuine Gemini failure (no parsed output + an error) -> flag the row as an
        # error/gemini outcome. A benign "picked nothing" (final_output not None) or an
        # uninvoked LLM must NOT set this.
        if final_output is None and final_error:
            llm_error_for_row = final_error
        final_url_selection_ai = {
            "provider": "google-gemini", "model": final_model,
            "used": final_output is not None, "error": final_error,
            "usage": final_usage or {}, "raw": final_output,
        }
        gemini_cost = calculate_gemini_cost_usd(final_usage)
        ai_website = final_output.get("official_website") if isinstance(final_output, dict) else None
        # Accept the LLM's validated pick (it's already constrained to candidates +
        # not-disallowed by choose_final_website_with_gemini). The domain-token
        # heuristic is NOT a gate here — it only adds a domain_name_mismatch flag
        # (carried in final_url_selection_ai.raw), so correct brand/abbreviation
        # domains aren't silently dropped.
        if (isinstance(ai_website, str) and ai_website.strip()
                and not is_disallowed_official_url(ai_website)):
            official_website = ai_website.strip()

    summary_text = (
        f"Modular Search Phase: {phase} completed. "
        f"Executed {len(queries)} query variations. "
        f"Found {len(candidates)} unique candidates."
    )
    
    deduped = dedupe_candidate_urls(candidates)
    crawl_resp = CrawlResponse(
        company_name=company_name,
        country=country,
        firm_id=firm_id,
        input_industry=input_industry,
        input_full_address=input_full_address,
        official_website=official_website,
        summary=summary_text,
        address=input_full_address,
        phone=None,
        email=None,
        industry=input_industry,
        products=[],
        services=[],
        website_company_descirption_ai=None,
        website_company_descirption_translated_ai=None,
        massive_proxy_cost_usd=0.0,
        serpwow_cost_usd=serpwow_cost,
        gemini_cost_usd=gemini_cost,
        total_cost_usd=serpwow_cost + gemini_cost,
        context={
            "pipeline": PIPELINE_GSEARCH,
            "phase": phase,
            "success": bool(official_website),
            "used_proxy": False,
            "blocked": False,
            "candidates": deduped,
            "skip_llm": skip_llm,
            "search_attempts": search_attempts,
            "formatted_results": formatted_results,
            "final_url_selection_ai": final_url_selection_ai,
            **({"llm_error": llm_error_for_row} if llm_error_for_row else {}),
            "cost_breakdown": {
                "massive_proxy_cost_usd": 0.0,
                "serpwow_cost_usd": serpwow_cost,
                "gemini_cost_usd": gemini_cost,
                "total_cost_usd": serpwow_cost + gemini_cost,
                "serpwow_request_count": len(queries),
                "serpwow_billable_request_count": billable_requests,
            }
        }
    )

    unified_raw_serpwow = {
        "queries": queries,
        "candidates": deduped,
        "results": formatted_results,
    }
    
    return crawl_resp, json.dumps(unified_raw_serpwow)
