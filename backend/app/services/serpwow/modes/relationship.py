# backend/app/services/serpwow/modes/relationship.py
"""relationship mode executor (SerpWow AI Overview, X↔Y financial relationship).

Per unique (X, Y) pair: fire the parallel phase queries, pool candidates
(X-domain blacklisted) + AI-overview evidence, then either call Gemini per-pair
(RELATIONSHIP_LLM_BATCH=false) or leave the verdict to the chunked Gemini batch
at finalization (=true). The relationship verdict GATES the URL — see
gemini_llm.apply_relationship_gate.
"""
from __future__ import annotations

import asyncio
import json
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
from app.services.serpwow.cost import calculate_gemini_cost_usd
from app.services.serpwow.gemini_llm import (
    apply_relationship_gate,
    choose_relationship_and_website,
)
from app.services.serpwow.outcomes import categorize_http_error
from app.services.serpwow.query_builders import build_relationship_phase_queries
from app.services.serpwow.schemas import CrawlResponse
from app.services.serpwow.serpwow_client import run_serpwow_search
from app.services.serpwow.url_utils import (
    dedupe_candidate_urls,
    is_disallowed_official_url,
    url_matches_domain,
    x_domain_from_input_url,
)


def _overview_text(raw_response: Any) -> str:
    if not isinstance(raw_response, dict):
        return ""
    overview = raw_response.get("ai_overview")
    if not isinstance(overview, dict):
        return ""
    contents = overview.get("ai_overview_contents")
    if not isinstance(contents, list):
        return ""
    return " ".join(
        (item.get("text") or "").strip()
        for item in contents if isinstance(item, dict)
    ).strip()


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
    max_phases = max(1, _get_int_env("RELATIONSHIP_MAX_PHASES", 4))
    queries = build_relationship_phase_queries(
        x_name=x_name, y_name=y_name, city=city, country=country,
        x_domain=x_domain, max_phases=max_phases)

    timeout_sec = _get_float_env("SERPWOW_TIMEOUT_SEC", 45.0)
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        results = await asyncio.gather(
            *[run_serpwow_search(q, country=country, client=client)
              for _, q in queries],
            return_exceptions=True,
        )

    candidates: list[str] = []
    seen: set[str] = set()
    ai_overview_texts: list[str] = []
    seen_overview_texts: set[str] = set()
    search_attempts: list[dict[str, Any]] = []
    formatted_results: list[dict[str, Any]] = []
    phase4_hit = False
    serpwow_cost = 0.0

    for (label, query), raw_result in zip(queries, results):
        if isinstance(raw_result, Exception):
            raw_result = {
                "provider": "serpwow", "used": False, "query": query,
                "official_website": None, "candidates": [], "status_code": None,
                "search_url": None, "raw_response": None,
                "error": f"{type(raw_result).__name__}: {raw_result}",
                "error_category": categorize_http_error(
                    None, f"{type(raw_result).__name__}: {raw_result}"),
            }
        serpwow_cost += 0.02
        raw_response = raw_result.get("raw_response")

        if label == "phase4_portfolio_anchor":
            # Phase 4 confirms Y appears on X's own site — relationship EVIDENCE,
            # never URL candidates (they're X's pages by construction).
            organic = (raw_response or {}).get("organic_results") if isinstance(raw_response, dict) else None
            phase4_hit = bool(organic)
        else:
            for cand in raw_result.get("candidates") or []:
                if (cand and cand not in seen
                        and not is_disallowed_official_url(cand)
                        and not url_matches_domain(cand, x_domain)):
                    seen.add(cand)
                    candidates.append(cand)

        text = _overview_text(raw_response)
        if text and text not in seen_overview_texts:
            seen_overview_texts.add(text)
            ai_overview_texts.append(text)

        formatted_results.append({
            "phase": label, "query": query,
            "success": bool(raw_result.get("used")),
            "error": raw_result.get("error"),
            "error_category": raw_result.get("error_category"),
            "status_code": raw_result.get("status_code"),
            "search_url": raw_result.get("search_url"),
            "raw_response": raw_response,
        })
        search_attempts.append({
            "attempt": label, "query": query,
            "search_url": raw_result.get("search_url"),
            "status": "candidates_found" if raw_result.get("candidates") else "no_candidates",
            "status_code": raw_result.get("status_code"),
            "error": raw_result.get("error"),
        })

    deduped = dedupe_candidate_urls(candidates)
    has_evidence = bool(deduped or ai_overview_texts)
    has_x = bool(str(x_name or "").strip())
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
    elif not has_evidence:
        # Pure-noise OCR: nothing to judge (spec §3.2).
        skip_llm = True
        row_error = REL_ERROR_NO_EVIDENCE
        relationship.update(status="not_confirmed",
                            summary="All phases returned no candidates and no AI overview.")
        relationship["flags"].append(
            {"flag": "no_evidence", "why": "no candidates and no AI-overview text from any phase"})
    elif not batch_mode:
        parsed, error, model, usage = await asyncio.to_thread(
            choose_relationship_and_website,
            x_name, y_name, city, country,
            deduped, ai_overview_texts, search_attempts, phase4_hit)
        if parsed is None:
            # LLM failure: the gate cannot be guessed — fail the row (retryable).
            raise RuntimeError(f"relationship LLM error: {error}")
        gated_url, status, gate_flags = apply_relationship_gate(parsed, deduped, x_domain)
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
        f"{len(queries)} phase queries, {len(deduped)} candidates, "
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
            "candidates": deduped,
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
                "serpwow_request_count": len(queries),
            },
        },
    )
    unified_raw = {"queries": queries, "candidates": deduped,
                   "results": formatted_results}
    return crawl_resp, json.dumps(unified_raw)
