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


# Keys dropped from the evidence text. This is an EXCLUDE list on purpose — the inverse of
# the include-list that kept losing content (first `list` items, then `snippet_links`). A key
# scrape.do adds tomorrow is unknown to this set, so it survives; only these named ones go:
#   search_parameters — our own ~1.2KB prompt echoed back. The model is already given the
#     task; re-reading our instructions as "evidence" is pure token cost, and it is where the
#     https://example.com format example and X's portfolio URL live.
#   type/level/index/reference_indexes — structural metadata, never prose. Their VALUES are
#     block-type names and numbers ("paragraph", 3), so emitting them adds noise, not text.
_NON_EVIDENCE_KEYS = frozenset(
    {"search_parameters", "type", "level", "index", "reference_indexes"})


def evidence_text(envelope: dict[str, Any]) -> str:
    """Every string in the provider's response, in order, one per line.

    Plain text, not JSON: the model reads the answer, not our serialisation of it, and the
    braces/quotes/indentation were ~25% of the evidence tokens on every row.

    Nothing is selected BY key. It walks whatever is there and emits each string it finds at
    any depth, so `snippet`, `list`/`ordered_list` items, `snippet_links[].text` and
    `snippet_links[].link` all come through — the last one being the resolved target of an
    inline link, and on a real 100-row run the ONLY link source scrape.do gave us, since
    `references[]` came back empty for all 100 rows.
    """
    payload = envelope.get("response")
    if not isinstance(payload, dict):
        payload = envelope
    lines: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.strip():
                lines.append(node.strip())
        elif isinstance(node, dict):
            for key, value in node.items():
                if key not in _NON_EVIDENCE_KEYS:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        # numbers/bools/None: no prose in them, and a bare "3" is noise to the reader.

    walk(payload)
    return "\n".join(lines)


def extract_https_urls(text: str) -> list[str]:
    """Every https:// URL in the evidence text, wherever it sat in the response.

    Because that text is every string the response contained, this collects URLs typed into
    the prose (what the search prompt asks for), ``snippet_links[].link`` (where AI Mode puts
    the target when it renders the website as linked text instead) and ``references[].link``
    — without knowing which key any of them came from.
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

    The evidence text is every string in the response (see evidence_text). The candidate set
    is every https:// URL in that same text — prose, ``snippet_links``, ``references`` —
    minus directory/social/file URLs and minus Company X's own domain. That set is what the
    gate validates the model's answer against, so it is the thing that stops a
    hallucinated URL being reported as Company Y's website.

    One text, one candidate source: since ``search_parameters`` is already excluded from it,
    the ``https://example.com`` the prompt uses to show the required format and X's own
    portfolio URL cannot leak into the allow-list as pickable websites for Company Y.
    """
    blocks, references = ai_mode_arrays(envelope)
    text = evidence_text(envelope)

    sources: list[dict[str, str]] = []
    for ref in references:
        if isinstance(ref, dict) and str(ref.get("link") or "").strip():
            link = str(ref["link"]).strip()
            sources.append({
                "name": str(ref.get("title") or ref.get("source") or link).strip(),
                "url": link,
            })

    candidates = dedupe_candidate_urls([
        c for c in extract_https_urls(text)
        if c and not is_disallowed_official_url(c) and not url_matches_domain(c, x_domain)
    ])

    error = envelope.get("error")
    # "Did AI Mode answer" is a question about the BLOCKS, not about the evidence text: a
    # response whose text_blocks came back empty can still have non-empty text.
    answered = bool(blocks)
    ai_overview_evidence: list[dict[str, Any]] = []
    if blocks or references:
        ai_overview_evidence.append({
            "phase": "ai_mode",
            # No "query" key: the response's own search_parameters.q inside `text` IS the
            # query, verbatim. Repeating our ~1.2KB prompt as a header — and again in the
            # search_attempts dump — cost three copies of it in every row's verdict prompt.
            "text": text,
            "sources": sources,
        })

    if error:
        status = "error"
        result_summary = str(error)
    elif candidates:
        status = "candidates_found"
        result_summary = (f"{'AI Mode answered' if answered else 'No AI Mode text'}; "
                          f"{len(candidates)} candidate(s)")
    else:
        status = "no_candidates"
        result_summary = (f"{'AI Mode answered' if answered else 'No AI Mode text'}; "
                          f"0 candidates")

    search_attempts = [{
        "attempt": "ai_mode",
        "status": status,
        "error": error,
        "result": result_summary,
        "ai_overview_present": answered,
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
