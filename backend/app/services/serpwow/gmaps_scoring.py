# backend/app/services/serpwow/gmaps_scoring.py
"""Google-Maps candidate scoring + heuristic confidence for the gmaps mode."""
from __future__ import annotations

import re
from typing import Any, Optional

from app.services.serpwow.url_utils import is_disallowed_official_url
from app.services.serpwow.address import (
    _address_evidence_markers,
    _extract_address_numbers,
    _is_address_aligned,
    _marker_matches_candidate,
    _meaningful_company_tokens,
    _normalize_address_match_text,
)

def _score_gmaps_candidates(
    gmaps_result: dict[str, Any],
    company_name: str,
    input_full_address: Optional[str],
) -> list[dict[str, Any]]:
    """Score every Google-Maps result that carries a website and expose the
    boolean signals used for heuristic confidence. Arithmetic matches the
    original _select_best_gmaps_website exactly (pure function)."""
    results = (gmaps_result or {}).get("results") or []
    if not isinstance(results, list):
        return []

    company_tokens = _meaningful_company_tokens(company_name)
    input_address = (input_full_address or "").strip()
    address_markers = _address_evidence_markers(input_address, max_markers=8) if input_address else []
    address_numbers = _extract_address_numbers(input_address) if input_address else []
    company_text = re.sub(r"[^a-z0-9]+", " ", (company_name or "").lower()).strip()

    organizational_terms = (
        "chamber", "embassy", "consular", "center", "centre", "ministry",
        "association", "council", "university", "college", "school", "hospital",
    )

    scored: list[dict[str, Any]] = []
    for idx, item in enumerate(results):
        if not isinstance(item, dict):
            continue
        raw_url = str(item.get("website") or "").strip()
        if not raw_url:
            continue
        candidate_url = re.sub(r"#.*$", "", raw_url).strip()
        if is_disallowed_official_url(candidate_url):
            continue

        title = str(item.get("title") or item.get("name") or "")
        category = str(item.get("type") or item.get("category") or "")
        address = str(item.get("address") or "")

        title_norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        category_norm = re.sub(r"[^a-z0-9]+", " ", category.lower()).strip()
        address_norm = _normalize_address_match_text(address)

        name_hits = sum(1 for token in company_tokens if token in title_norm) if company_tokens else 0
        number_hits = (sum(1 for number in address_numbers
                           if re.search(rf"\b{re.escape(number)}\b", address_norm))
                       if (address_norm and address_numbers) else 0)
        marker_hits = (sum(1 for marker in address_markers
                           if _marker_matches_candidate(marker, address_norm))
                       if (address_norm and address_markers) else 0)
        aligned: Optional[bool] = None
        if input_address and address:
            aligned = _is_address_aligned(input_address, address)
        org_mismatch = (
            any(term in f"{title_norm} {category_norm}" for term in organizational_terms)
            and not any(term in company_text for term in organizational_terms)
        )

        score = 0.0
        score += name_hits * 2.0
        score += number_hits * 3.0
        score += marker_hits * 2.0
        if aligned is True:
            score += 2.0
        elif aligned is False:
            score -= 2.0
        if org_mismatch:
            score -= 4.0
        score -= (idx * 0.01)

        scored.append({
            "url": candidate_url,
            "score": score,
            "name_match": name_hits > 0,
            "address_match": (number_hits > 0 or marker_hits > 0 or aligned is True),
            "address_conflict": aligned is False,
            "organizational_mismatch": org_mismatch,
            "idx": idx,
        })
    return scored


def _select_best_gmaps_website(
    gmaps_result: dict[str, Any],
    company_name: str,
    input_full_address: Optional[str],
) -> Optional[str]:
    scored = _score_gmaps_candidates(gmaps_result, company_name, input_full_address)
    if not scored:
        return None
    # max() returns the FIRST element with the top score; the idx tiebreaker
    # (-0.01*idx) already orders ties by original position — identical to the
    # original strict-greater loop.
    best = max(scored, key=lambda e: e["score"])
    return best["url"]


def _gmaps_confidence_for_entry(entry: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Map a scored gmaps candidate (or None) to a heuristic confidence 'raw'
    block matching the shape reporting reads."""
    if not entry or not entry.get("url"):
        return {
            "official_website": None, "confidence_score": 0, "confidence": "low",
            "reason": "No Google Maps listing with a usable website matched.",
            "name_match": False, "address_match": False,
            "address_conflict": False, "organizational_mismatch": False,
        }
    name = bool(entry.get("name_match"))
    addr = bool(entry.get("address_match"))
    if name and addr:
        score, reason = 90, "Google Maps listing matched the company name and address."
    elif name:
        score, reason = 70, "Google Maps listing matched the company name."
    elif addr:
        score, reason = 60, "Google Maps listing matched the address."
    else:
        score, reason = 40, "Google Maps listing found but neither name nor address corroborated it."
    if entry.get("address_conflict"):
        score -= 15
        reason += " Address appears to conflict with the input."
    if entry.get("organizational_mismatch"):
        score -= 20
        reason += " Listing looks like a different organization type."
    score = max(0, min(100, score))
    band = "high" if score >= 80 else "medium" if score >= 50 else "low"
    return {
        "official_website": entry["url"], "confidence_score": score, "confidence": band,
        "reason": reason, "name_match": name, "address_match": addr,
        "address_conflict": bool(entry.get("address_conflict")),
        "organizational_mismatch": bool(entry.get("organizational_mismatch")),
    }


def _gmaps_confidence_block(
    gmaps_result: dict[str, Any],
    company_name: str,
    input_full_address: Optional[str],
    chosen_url: Optional[str],
) -> dict[str, Any]:
    """Heuristic confidence block for the chosen gmaps URL.

    Computed for every gmaps row inside execute_gmaps_lookup, and the pipeline's ONLY
    confidence source since the LLM modes were removed in 2026-08."""
    scored = _score_gmaps_candidates(gmaps_result, company_name, input_full_address)
    entry: Optional[dict[str, Any]] = None
    if chosen_url:
        norm = re.sub(r"#.*$", "", str(chosen_url)).strip()
        entry = next((e for e in scored if e["url"] == norm), None)
        if entry is None:
            entry = {"url": norm, "name_match": False, "address_match": False,
                     "address_conflict": False, "organizational_mismatch": False}
    return {"raw": _gmaps_confidence_for_entry(entry), "mode": "heuristic"}
