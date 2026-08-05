# backend/app/services/serpwow/modes/relationship.py
"""Relationship row logic over scrape.do Google AI Mode evidence.

One AI Mode call per row supplies the evidence (text_blocks + references);
build_relationship_prompt, apply_relationship_gate and update_relationship_block turn
it into a verdict.

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
    """The three logical fields the prompt needs, from arbitrary CSV headers.

    Three, not five: an OCR'd portfolio page yields a Company X name, a Company Y name
    and the page's own URL — no location. City/country were carried over from the
    SerpWow pipeline, where they narrowed a keyword search; nothing supplies them here.

    Lives here rather than in relationship_runner so relationship_outputs can use it
    without importing the runner — the runner imports write_outputs, so the reverse
    direction would be a circular import.
    """
    return {
        "row_index": row.get("row_index"),
        "x_name": _column(row, "company_name_x", "company_x"),
        "y_name": _column(row, "company_name_y", "company_y"),
        "input_url": _column(row, "input_url"),
    }


def ai_mode_arrays(envelope: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """(text_blocks, references) out of one row's stored envelope.

    They live inside ``envelope["response"]`` — scrape.do's body kept verbatim, so the
    raw/ object is an exact copy of what the provider sent. Envelopes written before
    that change inlined the two arrays at the top level; the fallback reads those.
    """
    payload = envelope.get("response")
    if not isinstance(payload, dict):
        payload = envelope
    blocks = payload.get("text_blocks")
    refs = payload.get("references")
    return (blocks if isinstance(blocks, list) else [],
            refs if isinstance(refs, list) else [])


def flatten_text_blocks(blocks: list[Any]) -> str:
    """Every block AI Mode returned, in order, with its structure kept.

    Reading only each block's ``snippet`` silently dropped the most valuable part of the
    answer: a ``list`` block has no ``snippet`` at all — its content lives in ``list`` —
    and that is exactly where AI Mode puts the EVIDENCE bullets with the dates, amounts,
    round names and source attributions. The verdict LLM was being asked to confirm a
    financial relationship while the citations proving it were thrown away.

    Headings are kept as headings so the answer's own sections survive, and list items
    become bullets. Recursive, because a list item may itself carry a nested list.
    """
    lines: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if isinstance(node, str):
            if node.strip():
                lines.append(f"{'  ' * depth}- {node.strip()}")
            return
        if not isinstance(node, dict):
            return
        snippet = str(node.get("snippet") or "").strip()
        kind = str(node.get("type") or "")
        children = node.get("list")
        if kind == "heading" and snippet:
            lines.append("")
            lines.append(f"{snippet}")
        elif snippet:
            lines.append(f"{'  ' * depth}- {snippet}" if depth else snippet)
        if isinstance(children, list):
            for child in children:
                walk(child, depth + 1 if snippet else depth)

    for block in blocks or []:
        walk(block)
    return "\n".join(lines).strip()


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
    blocks, references = ai_mode_arrays(envelope)
    text = flatten_text_blocks(blocks)

    sources: list[dict[str, str]] = []
    raw_candidates: list[str] = []
    for ref in references:
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
        # A billed 200 that returned nothing. The run-level count comes off the
        # envelope in relationship_outputs; this copy is what the verdict prompt sees.
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

    blocks, _refs = ai_mode_arrays(envelope)
    has_x = bool(str(row.get("x_name") or "").strip())
    has_evidence = bool(candidates or blocks)

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
        "attempt_log": build_attempt_log(envelope, candidates),
    }


def build_attempt_log(envelope: dict[str, Any], candidates: list[str]) -> str:
    """The row's audit trail, one line per fact, for the output CSVs' attempt_log cell.

    There is a single attempt per row rather than a per-phase list — but "one attempt"
    is not "nothing worth recording": without this there is no way to tell a verdict
    reached on 12 references from one reached on an empty response, which is exactly
    what you need when judging the prompt.

    Newline-joined, matching EntityResult.attempt_log_csv: each line renders on its own
    row INSIDE one quoted CSV cell.
    """
    blocks, refs = ai_mode_arrays(envelope)
    lines = [
        f"provider: scrape.do google/search/ai-mode",
        f"attempts: {envelope.get('request_count') or 0} "
        f"(billed 200s: {envelope.get('successful_requests') or 0}, "
        f"credits: {envelope.get('credits') or 0})",
        f"ai_mode_text_blocks: {len(blocks)}",
        f"ai_mode_references: {len(refs)}",
        f"candidates_after_filtering: {len(candidates)}",
    ]
    if envelope.get("billed_empty"):
        lines.append("billed_empty: HTTP 200 with no text and no references")
    if envelope.get("error"):
        lines.append(f"error: {envelope['error']}")
    # The candidate set the gate was allowed to pick from — the single most useful thing
    # when a confirmed row came back with no URL.
    lines.extend(f"candidate: {url}" for url in candidates[:10])
    if len(candidates) > 10:
        lines.append(f"... and {len(candidates) - 10} more candidate(s)")
    query = str(envelope.get("query") or "")
    if query:
        lines.append(f"query: {query[:300]}")
    return "\n".join(lines)
