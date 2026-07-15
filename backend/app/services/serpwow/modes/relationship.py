# backend/app/services/serpwow/modes/relationship.py
"""relationship mode executor (SerpWow AI Overview, X↔Y financial relationship).

Per unique (X, Y) pair: run adaptive search phases, pool canonical candidates
(X-domain blacklisted) + evidence, then either call Gemini per-pair
(RELATIONSHIP_LLM_BATCH=false) or leave the verdict to the chunked Gemini batch
at finalization (=true). The relationship verdict GATES the URL — see
gemini_llm.apply_relationship_gate.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

import httpx

from app.services.common.env import (
    get_bool_env as _get_bool_env,
    get_float_env as _get_float_env,
    get_int_env as _get_int_env,
)
from app.services.serpwow.constants import (
    PIPELINE_RELATIONSHIP,
    REL_ERROR_CONFIRMED_URL_INVALID,
    REL_ERROR_NO_EVIDENCE,
    REL_ERROR_NO_X,
    REL_ERROR_NOT_CONFIRMED,
)
from app.services.serpwow.cost import (
    calculate_gemini_cost_usd,
    calculate_serpwow_cost_usd,
)
from app.services.serpwow.gemini_llm import (
    apply_relationship_gate,
    choose_relationship_and_website,
)
from app.services.serpwow.outcomes import SRC_GEMINI
from app.services.serpwow.relationship_search import (
    RelationshipSearchInput,
    RelationshipSearchResult,
    normalize_search_policy,
    run_relationship_phases,
)
from app.services.serpwow.schemas import CrawlResponse
from app.services.serpwow.serpwow_client import run_serpwow_search
from app.services.serpwow.url_utils import x_domain_from_input_url


async def execute_relationship_lookup_for_worker(
    y_name: str,
    x_name: str,
    input_url: str,
    city: str,
    country: str,
    debug_upload_id: Optional[str] = None,
    debug_row_index: Optional[int] = None,
) -> tuple[CrawlResponse, str]:
    x_domain = x_domain_from_input_url(input_url)
    has_x = bool(str(x_name or "").strip())
    search_input = RelationshipSearchInput(
        x_name=x_name,
        y_name=y_name,
        input_url=input_url,
        x_domain=x_domain,
        city=city,
        country=country,
    )
    if has_x:
        timeout_sec = _get_float_env("SERPWOW_TIMEOUT_SEC", 45.0)
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            async def search(query: str) -> dict[str, Any]:
                return await run_serpwow_search(
                    query, country=country, client=client)

            search_result = await run_relationship_phases(
                search,
                search_input,
                normalize_search_policy(
                    os.getenv("RELATIONSHIP_SEARCH_POLICY", "adaptive")),
                max(1, _get_int_env("RELATIONSHIP_MAX_PHASES", 3)),
            )
    else:
        search_result = RelationshipSearchResult(
            executed_phases=[], queries=[], candidates=[], candidate_evidence=[],
            evidence=[], search_attempts=[], formatted_results=[],
            relationship_evidenced=False, x_site_hit=False,
            successful_requests=0,
        )

    candidates = search_result.candidates
    ai_overview_texts = [record["text"] for record in search_result.evidence]
    search_attempts = search_result.search_attempts
    formatted_results = search_result.formatted_results
    phase4_hit = search_result.x_site_hit
    serpwow_cost = calculate_serpwow_cost_usd(search_result.request_count)
    has_evidence = bool(candidates or search_result.evidence)
    batch_mode = _get_bool_env("RELATIONSHIP_LLM_BATCH", False)

    skip_llm = False
    row_error: Optional[str] = None
    official_website: Optional[str] = None
    gemini_cost = 0.0
    relationship: dict[str, Any] = {
        "status": "pending", "summary": "",
        "verified_pair": f"{x_name} ↔ {y_name}", "flags": [],
    }
    final_url_selection_ai: dict[str, Any] = {
        "provider": "google-gemini", "model": None, "used": False,
        "error": "Skipped: batch mode or short-circuit.", "usage": {}, "raw": None,
    }

    if not has_x:
        # Gate can never pass without X — don't spend tokens (spec §2.1).
        skip_llm = True
        row_error = REL_ERROR_NO_X
        relationship.update(status="not_confirmed",
                            summary="Company X missing on this row.")
        relationship["flags"].append(
            {"flag": "no_company_x", "why": "row has no Company_Name_X to verify against"})
    elif (search_result.request_count > 0
          and search_result.successful_requests == 0):
        skip_llm = True
        relationship.update(
            status="unclear",
            summary="All relationship search phases failed technically.",
        )
        relationship["flags"].append({
            "flag": "all_search_phases_failed",
            "why": "no SerpWow request completed successfully",
        })
    elif not has_evidence:
        # Pure-noise OCR: nothing to judge (spec §3.2).
        skip_llm = True
        row_error = REL_ERROR_NO_EVIDENCE
        relationship.update(status="not_confirmed",
                            summary="All phases returned no candidates or usable evidence.")
        relationship["flags"].append(
            {"flag": "no_evidence", "why": "no candidates or evidence from any phase"})
    elif not batch_mode:
        parsed, error, model, usage = await asyncio.to_thread(
            choose_relationship_and_website,
            x_name, y_name, city, country,
            candidates, ai_overview_texts, search_attempts, phase4_hit, x_domain)
        if parsed is None:
            # LLM failure: the gate cannot be guessed — fail the row (retryable).
            # Tag the source so the worker's classify_exception attributes it to
            # gemini, not the default server source.
            err = RuntimeError(f"relationship LLM error: {error}")
            err.error_source = SRC_GEMINI
            raise err
        gated_url, status, gate_flags = apply_relationship_gate(
            parsed, candidates, x_domain)
        gemini_cost = calculate_gemini_cost_usd(usage)
        official_website = gated_url
        relationship.update(
            status=status,
            summary=str(parsed.get("relationship_summary") or ""),
        )
        relationship["flags"].extend(gate_flags)
        for extra in parsed.get("extra_flags") or []:
            if isinstance(extra, str) and extra.strip():
                relationship["flags"].append({"flag": extra.strip(), "why": "reported by LLM"})
        final_url_selection_ai = {
            "provider": "google-gemini", "model": model, "used": True,
            "error": None, "usage": usage or {}, "raw": parsed,
        }
        if official_website is None:
            row_error = REL_ERROR_NOT_CONFIRMED if status != "confirmed" else (
                REL_ERROR_CONFIRMED_URL_INVALID)
    # batch_mode with evidence: leave verdict to the finalization batch.

    summary_text = (
        f"Relationship search for pair {x_name!r} ↔ {y_name!r}: "
        f"{search_result.request_count} phase queries, {len(candidates)} candidates, "
        f"{len(ai_overview_texts)} AI-overview texts, phase4_hit={phase4_hit}."
    )
    crawl_resp = CrawlResponse(
        company_name=y_name,
        country=country,
        firm_id=None,
        input_industry=None,
        input_full_address=None,
        official_website=official_website,
        summary=summary_text,
        address=None, phone=None, email=None, industry=None,
        products=[], services=[],
        website_company_descirption_ai=None,
        website_company_descirption_translated_ai=None,
        massive_proxy_cost_usd=0.0,
        serpwow_cost_usd=serpwow_cost,
        gemini_cost_usd=gemini_cost,
        total_cost_usd=serpwow_cost + gemini_cost,
        context={
            "pipeline": PIPELINE_RELATIONSHIP,
            "success": bool(official_website),
            "used_proxy": False, "blocked": False,
            "x_name": x_name,
            "x_domain": x_domain,
            "phase4_hit": phase4_hit,
            "executed_phases": search_result.executed_phases,
            "candidates": candidates,
            "candidate_evidence": search_result.candidate_evidence,
            "evidence": search_result.evidence,
            "ai_overview_texts": ai_overview_texts,
            "search_attempts": search_attempts,
            "formatted_results": formatted_results,
            "skip_llm": skip_llm,
            "row_error": row_error,
            "relationship": relationship,
            "final_url_selection_ai": final_url_selection_ai,
            "cost_breakdown": {
                "massive_proxy_cost_usd": 0.0,
                "serpwow_cost_usd": serpwow_cost,
                "gemini_cost_usd": gemini_cost,
                "total_cost_usd": serpwow_cost + gemini_cost,
                "serpwow_request_count": search_result.request_count,
            },
        },
    )
    unified_raw = {
        "executed_phases": search_result.executed_phases,
        "queries": search_result.queries,
        "candidates": candidates,
        "results": formatted_results,
    }
    return crawl_resp, json.dumps(unified_raw)
