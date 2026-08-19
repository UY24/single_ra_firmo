# backend/app/services/serpwow/outcomes.py
"""Row-outcome taxonomy for the reporting SerpWow pipelines (+ reused by AI Mode).

One authority for turning a finalized row / a raised exception into an
(outcome, error_source, error_category, error_detail, degraded_search) tuple.
Pure — no I/O. See docs/superpowers/specs/2026-07-09-error-vs-notfound-taxonomy-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.services.serpwow.constants import (
    REL_ERROR_CONFIRMED_URL_INVALID,
    REL_ERROR_NO_EVIDENCE,
    REL_ERROR_NO_X,
    REL_ERROR_NOT_CONFIRMED,
)

OUTCOME_FOUND = "found"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_ERROR = "error"

SRC_SERPWOW = "serpwow"
SRC_SCRAPEDO = "scrapedo"
SRC_GEMINI = "gemini"
SRC_SERVER = "server"

CAT_RATE_LIMIT = "rate_limit"
CAT_AUTH = "auth"
CAT_TIMEOUT = "timeout"
CAT_HTTP_5XX = "http_5xx"
CAT_NETWORK = "network"
CAT_PARSE = "parse"
CAT_INTERNAL = "internal"

GENERIC_NOT_FOUND = "Official website not found; target blocked/unreachable or no valid result."
BATCH_NOT_FOUND = "Official website not found after Gemini batch post-processing."

NOT_FOUND_SENTINELS = frozenset({
    REL_ERROR_NO_EVIDENCE, REL_ERROR_NO_X, REL_ERROR_NOT_CONFIRMED,
    REL_ERROR_CONFIRMED_URL_INVALID, GENERIC_NOT_FOUND, BATCH_NOT_FOUND,
})


@dataclass
class OutcomeInfo:
    outcome: str
    error_source: Optional[str] = None
    error_category: Optional[str] = None
    error_detail: Optional[str] = None
    degraded_search: bool = False

    @property
    def row_status(self) -> str:
        return "failed" if self.outcome == OUTCOME_ERROR else "completed"


def categorize_http_error(status_code: Optional[int], exc_repr: str) -> str:
    if status_code is not None:
        if status_code == 429:
            return CAT_RATE_LIMIT
        if status_code in (401, 403):
            return CAT_AUTH
        if 500 <= status_code <= 599:
            return CAT_HTTP_5XX
    low = (exc_repr or "").lower()
    if "429" in low or "rate limit" in low or "too many requests" in low:
        return CAT_RATE_LIMIT
    if "401" in low or "403" in low or "unauthor" in low or "forbidden" in low or "api key" in low:
        return CAT_AUTH
    if "timeout" in low or "timed out" in low:
        return CAT_TIMEOUT
    if "connect" in low or "name resolution" in low or "connection" in low or "network" in low:
        return CAT_NETWORK
    if "jsondecode" in low or "expecting value" in low or "parse" in low:
        return CAT_PARSE
    return CAT_INTERNAL


def classify_exception(exc: BaseException, *, default_source: str) -> OutcomeInfo:
    exc_repr = f"{type(exc).__name__}: {exc}"
    status = getattr(exc, "status_code", None)
    # A raised exception may carry an explicit source override (e.g. a relationship
    # per-pair Gemini failure tags itself SRC_GEMINI before raising to stay retryable).
    source = getattr(exc, "error_source", None) or default_source
    return OutcomeInfo(
        outcome=OUTCOME_ERROR,
        error_source=source,
        error_category=categorize_http_error(status, exc_repr),
        error_detail=exc_repr,
    )


def _phase_stats(
    result: dict[str, Any],
) -> tuple[int, int, int, Optional[str], Optional[str], Optional[str]]:
    """(total, succeeded, errored, dominant_category, first_error_detail, error_source).

    ``error_source`` is taken from the first errored phase that names one, so a
    pipeline running on a different provider (gmaps on scrape.do) is attributed to it
    instead of defaulting to SerpWow. None when no phase declares a source.
    """
    ctx = result.get("context") if isinstance(result.get("context"), dict) else {}
    fr = ctx.get("formatted_results") if isinstance(ctx.get("formatted_results"), list) else []
    total = succeeded = errored = 0
    cat_counts: dict[str, int] = {}
    first_detail: Optional[str] = None
    source: Optional[str] = None
    for f in fr:
        if not isinstance(f, dict):
            continue
        total += 1
        if f.get("success"):
            succeeded += 1
        if f.get("error") or f.get("error_category"):
            errored += 1
            if first_detail is None and f.get("error"):
                first_detail = str(f.get("error"))
            if source is None and f.get("error_source"):
                source = str(f.get("error_source"))
            cat = f.get("error_category")
            if cat:
                cat_counts[str(cat)] = cat_counts.get(str(cat), 0) + 1
    dominant = max(cat_counts, key=lambda k: cat_counts[k]) if cat_counts else None
    return total, succeeded, errored, dominant, first_detail, source


def classify_finalized_row(result: dict[str, Any], *, pipeline: str,
                           ctx_row_error: Optional[str], skip_llm: bool) -> OutcomeInfo:
    official = str((result or {}).get("official_website") or "").strip()
    total, succeeded, errored, dominant_cat, first_detail, phase_source = _phase_stats(
        result or {})
    degraded = bool(succeeded and errored)
    if pipeline == "firmographics":
        # firmographics is HANDED the website, so official_website is an echo of the
        # INPUT and says nothing about whether the row worked. Judging it the usual way
        # made every row "found" -- including rows whose provider call failed outright,
        # which then reported success at zero cost. Classify on what the row actually
        # produced instead: the provider erroring is an error, no AI overview to extract
        # from is a business not-found, anything else is found.
        if total > 0 and succeeded == 0:
            source = phase_source or SRC_SCRAPEDO
            return OutcomeInfo(OUTCOME_ERROR, source,
                               dominant_cat or CAT_INTERNAL,
                               error_detail=first_detail or f"{source} search failed")
        if not official:
            return OutcomeInfo(OUTCOME_NOT_FOUND)
        enriched = any(
            (result or {}).get(field) for field in
            ("address", "phone", "email", "industry", "products", "services"))
        return OutcomeInfo(OUTCOME_FOUND) if enriched else OutcomeInfo(OUTCOME_NOT_FOUND)
    if official:
        # gsearch falls back to the raw first candidate as official_website, so a
        # per-row Gemini SELECTION failure still yields a found row (no retry, no
        # SerpWow re-bill). Surface that failure by marking the row degraded -- the
        # llm_selection_failed flag + errors/ dump carry the detail.
        degraded_search = degraded or bool(((result or {}).get("context") or {}).get("llm_error"))
        return OutcomeInfo(OUTCOME_FOUND, degraded_search=degraded_search)
    # "We couldn't look": phases ran and every one errored -> a real provider error.
    if total > 0 and succeeded == 0:
        source = phase_source or SRC_SERPWOW
        return OutcomeInfo(OUTCOME_ERROR, source,
                           dominant_cat or CAT_INTERNAL,
                           error_detail=first_detail or f"all {source} phases errored")
    # Known business "not found" sentinels (e.g. relationship not-confirmed / no-evidence).
    # Checked after provider failure so "no evidence" cannot mask that we never looked.
    if ctx_row_error and ctx_row_error.strip() in NOT_FOUND_SENTINELS:
        return OutcomeInfo(OUTCOME_NOT_FOUND, degraded_search=degraded)
    # Otherwise we looked and found nothing.
    return OutcomeInfo(OUTCOME_NOT_FOUND, degraded_search=degraded)
