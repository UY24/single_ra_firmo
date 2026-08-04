# backend/app/services/serpwow/modes/relationship.py
"""Relationship row logic over scrape.do Google AI Mode evidence.

The LLM half of this pipeline is UNCHANGED — build_relationship_prompt,
apply_relationship_gate and update_relationship_block keep their rules. What changed in
the 2026-08 migration is only the evidence handed to them: one scrape.do AI Mode call
per row (text_blocks + references) instead of three SerpWow AI-Overview searches.

Everything here is a pure function. Orchestration lives in relationship_runner.py.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from app.services.serpwow.constants import (
    REL_ERROR_CONFIRMED_URL_INVALID,
    REL_ERROR_NO_EVIDENCE,
    REL_ERROR_NO_X,
    REL_ERROR_NOT_CONFIRMED,
)
from app.services.serpwow.gemini_llm import (
    apply_relationship_gate,
    update_relationship_block,
)
from app.services.serpwow.url_utils import (
    dedupe_candidate_urls,
    is_disallowed_official_url,
    url_matches_domain,
)


def _column(row: dict[str, Any], *aliases: str) -> str:
    """First matching column value, header-case-insensitively."""
    lowered = {str(k).strip().lower(): v for k, v in row.items() if isinstance(k, str)}
    for alias in aliases:
        value = lowered.get(alias)
        if value:
            return str(value).strip()
    return ""


def row_fields(row: dict[str, Any]) -> dict[str, Any]:
    """The five logical fields the prompt needs, from arbitrary CSV headers.

    Lives here rather than in relationship_runner so relationship_outputs can use it
    without importing the runner — the runner imports write_outputs, so the reverse
    direction would be a circular import.
    """
    return {
        "row_index": row.get("row_index"),
        "x_name": _column(row, "company_name_x", "company_x"),
        "y_name": _column(row, "company_name_y", "company_y"),
        "input_url": _column(row, "input_url"),
        "city": _column(row, "city", "town"),
        "country": _column(row, "country", "country_name", "nation"),
    }


def extract_https_urls(text: str) -> list[str]:
    """Plain-text https:// URLs typed into the AI Mode prose.

    The prompt asks for the website as text rather than a hyperlink precisely so it
    lands here, and not only in references[].
    """
    seen: set[str] = set()
    urls: list[str] = []
    for match in re.findall(r'https://[^\s<>"\']+', text or ""):
        url = match.rstrip(".,;:!?)]}")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def build_evidence(envelope: dict[str, Any], x_domain: str) -> dict[str, Any]:
    """Turn one AI Mode envelope into the arguments build_relationship_prompt expects.

    The candidate set is references[] plus any URL typed into the prose, minus
    directory/social/file URLs and minus Company X's own domain. That set is what the
    gate validates the model's answer against, so it is the thing that stops a
    hallucinated URL being reported as Company Y's website.
    """
    blocks = envelope.get("text_blocks") or []
    text = "\n\n".join(
        str(b.get("snippet") or "").strip()
        for b in blocks
        if isinstance(b, dict) and str(b.get("snippet") or "").strip()
    )

    sources: list[dict[str, str]] = []
    raw_candidates: list[str] = []
    for ref in envelope.get("references") or []:
        if not isinstance(ref, dict):
            continue
        link = str(ref.get("link") or "").strip()
        if not link:
            continue
        raw_candidates.append(link)
        sources.append({
            "name": str(ref.get("title") or ref.get("source") or link).strip(),
            "url": link,
        })
    raw_candidates.extend(extract_https_urls(text))

    candidates = dedupe_candidate_urls([
        c for c in raw_candidates
        if c and not is_disallowed_official_url(c) and not url_matches_domain(c, x_domain)
    ])

    error = envelope.get("error")
    ai_overview_evidence: list[dict[str, Any]] = []
    if text:
        ai_overview_evidence.append({
            "phase": "ai_mode",
            "query": envelope.get("query") or "",
            "text": text,
            "sources": sources,
        })

    if error:
        status = "error"
        result_summary = str(error)
    elif candidates:
        status = "candidates_found"
        result_summary = (f"{'AI Mode answered' if text else 'No AI Mode text'}; "
                          f"{len(candidates)} candidate(s)")
    else:
        status = "no_candidates"
        result_summary = (f"{'AI Mode answered' if text else 'No AI Mode text'}; "
                          f"0 candidates")

    search_attempts = [{
        "attempt": "ai_mode",
        "query": envelope.get("query") or "",
        "status": status,
        "error": error,
        "result": result_summary,
        "ai_overview_present": bool(text),
        "candidate_count": len(candidates),
        # Kept so empty_response_breakdown can spot a billed 200 that returned nothing.
        "billed_empty": bool(envelope.get("billed_empty")),
    }]

    return {
        "candidates": candidates,
        "ai_overview_evidence": ai_overview_evidence,
        "search_attempts": search_attempts,
        "overview_text": text,
    }


def build_row_result(
    row: dict[str, Any],
    envelope: dict[str, Any],
    parsed: Optional[dict[str, Any]],
    candidates: list[str],
    x_domain: str,
) -> dict[str, Any]:
    """Apply the unchanged gate and flatten one row into what the CSV writer needs.

    ``parsed`` is the Gemini Batch verdict JSON, or None when the model never ran
    (short-circuited row, no evidence, or a scrape failure).
    """
    relationship: dict[str, Any] = {"status": "pending", "summary": "", "flags": []}
    official_website: Optional[str] = None
    row_error: Optional[str] = None
    status = "unclear"

    has_x = bool(str(row.get("x_name") or "").strip())
    has_evidence = bool(candidates or envelope.get("text_blocks"))

    if envelope.get("error"):
        status = "not_confirmed"
        relationship.update(status=status, summary=str(envelope["error"]))
        relationship["flags"].append(
            {"flag": "scrapedo_failed", "why": str(envelope["error"])})
        row_error = str(envelope["error"])
    elif not has_x:
        # The gate can never pass without X — don't spend tokens on it.
        status = "not_confirmed"
        relationship.update(status=status, summary="Company X missing on this row.")
        relationship["flags"].append(
            {"flag": "no_company_x", "why": "row has no Company_Name_X to verify against"})
        row_error = REL_ERROR_NO_X
    elif not has_evidence:
        status = "not_confirmed"
        relationship.update(
            status=status,
            summary="AI Mode returned no text and no references for this row.")
        relationship["flags"].append(
            {"flag": "no_evidence", "why": "empty AI Mode response"})
        row_error = REL_ERROR_NO_EVIDENCE
    elif parsed is None:
        status = "unclear"
        relationship.update(status=status, summary="No LLM verdict for this row.")
        relationship["flags"].append(
            {"flag": "llm_missing", "why": "Gemini batch produced no verdict"})
        row_error = REL_ERROR_NOT_CONFIRMED
    else:
        official_website, status, gate_flags = apply_relationship_gate(
            parsed, candidates, x_domain)
        relationship = update_relationship_block(
            relationship, parsed, status, gate_flags)
        if official_website is None:
            row_error = (REL_ERROR_CONFIRMED_URL_INVALID if status == "confirmed"
                         else REL_ERROR_NOT_CONFIRMED)

    return {
        "row_index": row.get("row_index"),
        "official_website": official_website or "",
        "relationship_status": status,
        "relationship": relationship,
        "row_error": row_error or "",
        "error_source": "scrapedo" if envelope.get("error") else "",
        "candidates": candidates,
    }
