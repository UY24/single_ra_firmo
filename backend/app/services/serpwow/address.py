# backend/app/services/serpwow/address.py
"""Address parsing, locality/marker extraction, and address-alignment matching."""
from __future__ import annotations

import re
from typing import Any, Optional

def _extract_address_search_fragments(full_address: str, max_fragments: int = 3) -> list[str]:
    if not full_address:
        return []

    keyword_markers = (
        "plot",
        "road",
        "rd",
        "block",
        "house",
        "street",
        "st",
        "sector",
        "building",
        "tower",
        "avenue",
        "ave",
    )
    raw_parts = [part.strip() for part in re.split(r",|\||;|\n", full_address) if part.strip()]
    selected: list[str] = []
    seen: set[str] = set()

    for part in raw_parts:
        normalized = _normalize_location_token(part)
        if len(normalized) < 3:
            continue
        lowered = normalized.lower()
        has_digit = bool(re.search(r"\d", lowered))
        has_keyword = any(marker in lowered for marker in keyword_markers)
        if not (has_digit or has_keyword):
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        selected.append(normalized)
        if len(selected) >= max_fragments:
            break

    return selected


def _extract_address_component(full_address: str, keywords: tuple[str, ...]) -> str:
    parts = [_normalize_location_token(part) for part in re.split(r",|\|", full_address or "") if part.strip()]
    for part in parts:
        lowered = part.lower()
        if any(keyword in lowered for keyword in keywords):
            return part
    return ""


def _marker_variants(value: str) -> tuple[str, str]:
    marker = _normalize_location_token(value)
    if not marker:
        return "", ""
    spaced = re.sub(r"[-#:/]+", " ", marker)
    spaced = re.sub(r"\s+", " ", spaced).strip()
    words = spaced.split()
    if words:
        words[0] = words[0].title()
    spaced = " ".join(words).strip()
    hyphen = ""
    if words:
        if len(words) == 1:
            hyphen = words[0]
        else:
            suffix_tokens = words[1:]
            if any(token.startswith("(") and token.endswith(")") for token in suffix_tokens):
                hyphen = f"{words[0]}-{'-'.join([token for token in suffix_tokens if not (token.startswith('(') and token.endswith(')'))])}".strip("-")
                paren_tokens = [token for token in suffix_tokens if token.startswith("(") and token.endswith(")")]
                if paren_tokens:
                    hyphen = f"{hyphen} {' '.join(paren_tokens)}".strip()
            else:
                hyphen = f"{words[0]}-{'-'.join(words[1:])}"
    return hyphen.strip(), spaced.strip()


def _extract_locality_city_postal(
    full_address: str,
    parsed_city_state: str,
    country: str,
) -> tuple[str, str, str]:
    parts = _dedupe_location_parts(
        [_normalize_location_token(part) for part in re.split(r",|\|", full_address or "") if part.strip()]
    )
    country_key = _normalize_location_token(country).lower()
    city = ""
    locality = ""
    postal = ""
    parsed_parts = [part for part in _dedupe_location_parts(parsed_city_state.split()) if part]
    if parsed_parts:
        city = parsed_parts[0]

    street_keywords = ("plot", "road", "rd", "block", "house", "street", "sector", "building", "tower", "avenue")
    for part in parts:
        lowered = part.lower()
        if not postal:
            postal_match = re.search(r"\b\d{4,6}\b", part)
            if postal_match:
                postal = postal_match.group(0)
        if not city and part and not any(k in lowered for k in street_keywords):
            if lowered != country_key and not re.fullmatch(r"\d{3,6}", lowered):
                city = part
        if not locality and part and not any(k in lowered for k in street_keywords):
            if lowered not in {country_key, city.lower() if city else ""} and not re.fullmatch(r"\d{3,6}", lowered):
                locality = part

    if not city:
        city = locality
    if not locality:
        locality = city
    return locality, city, postal


def _normalize_location_token(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    text = re.sub(r"^[,;:\-]+|[,;:\-]+$", "", text).strip()
    return text


def _dedupe_location_parts(parts: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = _normalize_location_token(part)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _is_suspicious_city_state_value(value: str) -> bool:
    lowered = (value or "").lower()
    suspicious_keywords = (
        "block",
        "plot",
        "road",
        "street",
        "st.",
        "sector",
        "house",
        "building",
        "floor",
        "apt",
        "apartment",
    )
    return any(keyword in lowered for keyword in suspicious_keywords)


def _heuristic_city_state_from_full_address(full_address: str) -> tuple[str, str]:
    raw_parts = [part.strip() for part in re.split(r",|\|", full_address or "") if part.strip()]
    cleaned_parts: list[str] = []
    for part in raw_parts:
        # Drop portions heavily numeric (plot numbers, pin codes, etc).
        letters = re.sub(r"[^A-Za-z]", "", part)
        if len(letters) < 2:
            continue
        cleaned_parts.append(part)
    deduped = _dedupe_location_parts(cleaned_parts)
    if not deduped:
        return "", ""
    city = deduped[-1]
    state = deduped[-2] if len(deduped) >= 2 else ""
    if city.lower() == state.lower():
        state = ""
    return city, state


def _address_evidence_markers(full_address: str, max_markers: int = 5) -> list[str]:
    markers: list[str] = []
    if not full_address:
        return markers

    generic_parts = {"dhaka", "bangladesh", "city", "district", "road", "plot", "block", "house"}
    for part in [p.strip() for p in re.split(r",|\|", full_address) if p and p.strip()]:
        normalized = re.sub(r"[^a-z0-9]+", " ", part.lower()).strip()
        normalized = re.sub(r"\brd\b", "road", normalized)
        normalized = re.sub(r"\bst\b", "street", normalized)
        normalized = re.sub(r"\bave\b", "avenue", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if len(normalized) < 3:
            continue
        if normalized in generic_parts:
            continue
        # Postal-only numbers are weak evidence and create false positives.
        if re.fullmatch(r"\d{3,6}", normalized):
            continue
        has_digit = bool(re.search(r"\d", normalized))
        has_letter = bool(re.search(r"[a-z]", normalized))
        if has_digit or (has_letter and len(normalized) >= 6):
            markers.append(normalized)
        if len(markers) >= max_markers:
            break
    return markers


def _normalize_address_match_text(value: Optional[str]) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    normalized = re.sub(r"\brd\b", "road", normalized)
    normalized = re.sub(r"\bst\b", "street", normalized)
    normalized = re.sub(r"\bave\b", "avenue", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _marker_variants_for_match(marker: str) -> set[str]:
    base = _normalize_address_match_text(marker)
    if not base:
        return set()

    variants = {base}
    if "road" in base:
        variants.add(re.sub(r"\broad\b", "rd", base))
    if "rd" in base:
        variants.add(re.sub(r"\brd\b", "road", base))
    if "plot" in base:
        variants.add(re.sub(r"\bplot\b", "house", base))
    if "house" in base:
        variants.add(re.sub(r"\bhouse\b", "plot", base))
    return {re.sub(r"\s+", " ", value).strip() for value in variants if value and value.strip()}


def _marker_matches_candidate(marker: str, normalized_candidate: str) -> bool:
    if not marker or not normalized_candidate:
        return False
    variants = _marker_variants_for_match(marker)
    return any(variant in normalized_candidate for variant in variants)


def _is_address_aligned(input_full_address: str, candidate_address: Optional[str]) -> bool:
    if not input_full_address:
        return True
    if not candidate_address:
        return False
    markers = _address_evidence_markers(input_full_address)
    if not markers:
        return True
    normalized_candidate = _normalize_address_match_text(candidate_address)

    strong_keywords = ("plot", "house", "road", "street", "block", "sector", "building", "tower", "avenue")
    strong_markers = [
        marker
        for marker in markers
        if any(keyword in marker for keyword in strong_keywords)
        or (bool(re.search(r"[a-z]", marker)) and bool(re.search(r"\d", marker)))
    ]
    weak_markers = [marker for marker in markers if marker not in strong_markers]

    if strong_markers:
        return any(_marker_matches_candidate(marker, normalized_candidate) for marker in strong_markers)
    return any(_marker_matches_candidate(marker, normalized_candidate) for marker in weak_markers)


def _meaningful_company_tokens(company_name: str) -> list[str]:
    generic = {
        "company",
        "corporation",
        "limited",
        "ltd",
        "group",
        "trading",
        "co",
        "inc",
        "llc",
        "plc",
        "spa",
        "srl",
        "sp",
        "zoo",
        "z",
        "o",
        "oo",
    }
    return [
        token
        for token in re.findall(r"[a-z0-9]+", (company_name or "").lower())
        if len(token) >= 3 and token not in generic
    ]


def _extract_address_numbers(value: str) -> list[str]:
    numbers = list(dict.fromkeys(re.findall(r"\d{1,5}", value or "")))
    if not numbers:
        return numbers
    short_numbers = [num for num in numbers if len(num) <= 3]
    if short_numbers:
        # When specific short markers exist (plot/road/house numbers), de-prioritize postal codes.
        return short_numbers
    return numbers
