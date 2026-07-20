# backend/app/services/serpwow/query_builders.py
"""Search-query construction for SerpWow modes (primary/fallback + phase queries)."""
from __future__ import annotations

import re
from typing import Any, Optional

from app.services.serpwow.geo import _country_to_gl
from app.services.serpwow.address import (
    _extract_address_component,
    _extract_locality_city_postal,
    _marker_variants,
    _normalize_location_token,
)

def build_primary_search_query(company_name: str, country: str) -> str:
    return f'What is the official website of "{company_name}" in {country}?'


def build_industry_fallback_query(company_name: str, industry: str) -> str:
    return f'What is the official website of "{company_name}" in the {industry} industry?'


def build_address_fallback_query(company_name: str, full_address: str) -> str:
    return f'What is the official website of "{company_name}" at {full_address}?'


def _append_unique_attempt_query(
    attempt_queries: list[tuple[str, str]],
    seen_queries: set[str],
    label: str,
    query: str,
) -> None:
    normalized = re.sub(r"\s+", " ", (query or "").strip())
    if not normalized:
        return
    key = normalized.lower()
    if key in seen_queries:
        return
    seen_queries.add(key)
    attempt_queries.append((label, normalized))


def _company_name_variants(company_name: str) -> list[str]:
    base = _normalize_location_token(company_name)
    if not base:
        return []
    variants: list[str] = [base]
    initials_match = re.match(r"^\s*([A-Za-z](?:\s+[A-Za-z]){1,4})\s+(.+)$", base)
    if initials_match:
        letters = re.findall(r"[A-Za-z]", initials_match.group(1))
        tail = _normalize_location_token(initials_match.group(2))
        if letters and tail:
            dotted = ".".join(letters) + "."
            compact = "".join(letters)
            variants.append(f"{dotted} {tail}")
            variants.append(f"{compact} {tail}")
    deduped: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        key = variant.lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(variant.strip())
    return deduped


def _looks_like_person_name(value: str) -> bool:
    text = _normalize_location_token(value)
    if not text:
        return False
    lowered = text.lower()
    corporate_markers = (" ltd", " limited", " llc", " inc", " corporation", " corp", " company", " group")
    if any(marker in f" {lowered}" for marker in corporate_markers):
        return False
    tokens = re.findall(r"[A-Za-z][A-Za-z'.-]*", text)
    if len(tokens) < 2 or len(tokens) > 6:
        return False
    capitalized = sum(1 for token in tokens if token and token[0].isupper())
    return capitalized >= 2


def _extract_phase5_pivots_from_serpwow(raw_response: Optional[dict[str, Any]]) -> tuple[list[str], list[str]]:
    if not isinstance(raw_response, dict):
        return [], []

    titles: list[str] = []
    ai_overview = raw_response.get("ai_overview")
    if isinstance(ai_overview, dict):
        for source in ai_overview.get("ai_overview_sources", []) or []:
            if isinstance(source, dict):
                title = _normalize_location_token(str(source.get("source_title") or ""))
                if title:
                    titles.append(title)
    for item in raw_response.get("organic_results", []) or []:
        if isinstance(item, dict):
            title = _normalize_location_token(str(item.get("title") or ""))
            if title:
                titles.append(title)

    people: list[str] = []
    trade_names: list[str] = []
    seen_people: set[str] = set()
    seen_trade: set[str] = set()
    corporate_terms = (
        "ltd",
        "limited",
        "llc",
        "inc",
        "corp",
        "corporation",
        "group",
        "motors",
        "museum",
        "auto",
        "trading",
    )

    for title in titles:
        head = title.split(" - ", 1)[0].split("|", 1)[0].strip()
        head = _normalize_location_token(head)
        if not head:
            continue
        lowered = head.lower()
        if _looks_like_person_name(head):
            person_key = lowered
            if person_key not in seen_people:
                seen_people.add(person_key)
                people.append(head)
        elif any(term in lowered for term in corporate_terms):
            if "overview" in lowered or "profile" in lowered or "directory" in lowered:
                continue
            trade_key = lowered
            if trade_key not in seen_trade:
                seen_trade.add(trade_key)
                trade_names.append(head)

    return people[:3], trade_names[:3]


def build_investigative_search_queries(
    company_name: str,
    country: str,
    parsed_city_state: str = "",
    full_address: str = "",
    industry: str = "",
    max_queries: int = 10,
) -> list[tuple[str, str]]:
    attempt_queries: list[tuple[str, str]] = []
    seen_queries: set[str] = set()

    clean_company = _normalize_location_token(company_name)
    clean_country = _normalize_location_token(country)
    clean_city_state = _normalize_location_token(parsed_city_state)
    clean_industry = _normalize_location_token(industry)

    variants = _company_name_variants(clean_company)
    v_primary = variants[0] if variants else clean_company
    v_punct = variants[1] if len(variants) > 1 else v_primary
    v_compact = variants[2] if len(variants) > 2 else v_primary

    plot_raw = _extract_address_component(full_address, ("plot",))
    road_raw = _extract_address_component(full_address, ("road", " rd"))
    block_raw = _extract_address_component(full_address, ("block",))
    house_raw = _extract_address_component(full_address, ("house",))
    plot_hyphen, plot_space = _marker_variants(plot_raw)
    road_hyphen, road_space = _marker_variants(road_raw)
    block_hyphen, _ = _marker_variants(block_raw)
    house_hyphen, _ = _marker_variants(house_raw)
    if not house_hyphen and plot_hyphen:
        number_match = re.search(r"\b(\d{1,5})\b", plot_hyphen)
        if number_match:
            house_hyphen = f"House-{number_match.group(1)}"

    locality, city, postal = _extract_locality_city_postal(full_address, clean_city_state, clean_country)
    location_phrase = " ".join(part for part in [locality, city] if part).strip() or clean_city_state or clean_country

    # Phase 1: Initial Hook (exact-ish dorks with quoted markers).
    if plot_hyphen and road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase1_exact_hook",
            f'"{v_primary}" "{plot_hyphen}" "{road_hyphen}" {location_phrase}'.strip(),
        )
    if plot_space and road_space:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase1_punctuation_variation",
            f'"{v_punct}" "{plot_space}" "{road_space}" {location_phrase}'.strip(),
        )
    if block_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase1_block_variation",
            f'"{v_compact}" "{block_hyphen}" {location_phrase}'.strip(),
        )
    if city and postal:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase1_city_postal",
            f'"{v_primary}" "{city} {postal}"',
        )
    elif location_phrase:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase1_city_country",
            f'"{v_primary}" "{location_phrase}"',
        )

    # Phase 2: AI Overview triggers (natural language synthesis prompts).
    if plot_space and road_space:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase2_business_name_at_address",
            (
                f'What business or trade name operates under "{v_primary}" at '
                f"{plot_space}, {road_space}, {location_phrase}?"
            ),
        )
    _append_unique_attempt_query(
        attempt_queries,
        seen_queries,
        "phase2_exec_or_owner",
        f'Who is the owner, CEO, or Managing Director of "{v_primary}" located in {location_phrase}?',
    )
    if block_hyphen and plot_hyphen and road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase2_registered_companies",
            (
                "What companies are registered at the address "
                f'{block_hyphen}, {plot_hyphen}, {road_hyphen}, {location_phrase}?'
            ),
        )
    if road_space:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase2_consumer_website",
            (
                f"Is there a consumer-facing website for {v_primary} "
                f"registered at {road_space} {location_phrase}?"
            ),
        )

    # Phase 3: Pivot (address-only discovery).
    if plot_hyphen and road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase3_address_pivot",
            f'"{plot_hyphen}" "{road_hyphen}" {location_phrase} -residential',
        )
    if house_hyphen and road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase3_house_plot_variation",
            f'"{house_hyphen}" "{road_hyphen}" {location_phrase} company OR business',
        )
    if block_hyphen and plot_hyphen and road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase3_block_plot_road",
            f'"{block_hyphen}" "{plot_hyphen}" "{road_hyphen}" {location_phrase}',
        )

    # Phase 4: Document hunting (operator-first dorks).
    country_tld = _country_to_gl(clean_country)
    _append_unique_attempt_query(
        attempt_queries,
        seen_queries,
        "phase4_country_registry_docs",
        f'site:.{country_tld} "{v_primary}" "{(locality or city or clean_country)}"',
    )
    if plot_hyphen or road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase4_company_plot_road_docs",
            (
                f'filetype:pdf "{v_primary}" '
                f'"{plot_hyphen or plot_space or plot_raw}" OR "{road_hyphen or road_space or road_raw}"'
            ),
        )
    if plot_hyphen and road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase4_address_directory_docs",
            f'filetype:pdf "{plot_hyphen}" "{road_hyphen}" {(locality or city or clean_country)} directory',
        )

    if clean_industry:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "industry_fallback",
            build_industry_fallback_query(clean_company, clean_industry),
        )

    return attempt_queries[: max(1, max_queries)]


def build_selected_phase_queries(
    company_name: str,
    country: str,
    parsed_city_state: str = "",
    full_address: str = "",
    industry: str = "",
    phase: str = "all",
) -> list[tuple[str, str]]:
    attempt_queries: list[tuple[str, str]] = []
    seen_queries: set[str] = set()

    clean_company = _normalize_location_token(company_name)
    clean_country = _normalize_location_token(country)
    clean_city_state = _normalize_location_token(parsed_city_state)
    clean_industry = _normalize_location_token(industry)

    variants = _company_name_variants(clean_company)
    v_primary = variants[0] if variants else clean_company
    v_punct = variants[1] if len(variants) > 1 else v_primary
    v_compact = variants[2] if len(variants) > 2 else v_primary

    # Location markers and extraction
    plot_raw = _extract_address_component(full_address, ("plot",)) if full_address else ""
    road_raw = _extract_address_component(full_address, ("road", " rd")) if full_address else ""
    block_raw = _extract_address_component(full_address, ("block",)) if full_address else ""
    house_raw = _extract_address_component(full_address, ("house",)) if full_address else ""
    
    plot_hyphen, plot_space = _marker_variants(plot_raw) if plot_raw else ("", "")
    road_hyphen, road_space = _marker_variants(road_raw) if road_raw else ("", "")
    block_hyphen, _ = _marker_variants(block_raw) if block_raw else ("", "")
    house_hyphen, _ = _marker_variants(house_raw) if house_raw else ("", "")
    
    if not house_hyphen and plot_hyphen:
        number_match = re.search(r"\b(\d{1,5})\b", plot_hyphen)
        if number_match:
            house_hyphen = f"House-{number_match.group(1)}"

    locality, city, postal = _extract_locality_city_postal(full_address, clean_city_state, clean_country) if full_address else ("", "", "")
    location_phrase = " ".join(part for part in [locality, city] if part).strip() or clean_city_state or clean_country

    has_address = bool(full_address and full_address.strip())

    # Phase 1: Initial Hook & Punctuation Variations
    if phase in ("phase1", "all"):
        if has_address:
            if plot_hyphen and road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase1_exact_hook",
                    f'"{v_primary}" "{plot_hyphen}" "{road_hyphen}" {location_phrase}'.strip(),
                )
            if plot_space and road_space:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase1_punctuation_variation",
                    f'"{v_punct}" "{plot_space}" "{road_space}" {location_phrase}'.strip(),
                )
            if block_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase1_block_variation",
                    f'"{v_compact}" "{block_hyphen}" {location_phrase}'.strip(),
                )
            if city and postal:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase1_city_postal",
                    f'"{v_primary}" "{city} {postal}"',
                )
            elif location_phrase:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase1_city_country",
                    f'"{v_primary}" "{location_phrase}"',
                )
        else:
            # Address is not available, only Country is available
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase1_no_address_primary",
                f'"{v_primary}" {clean_country}',
            )
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase1_no_address_punct",
                f'"{v_punct}" {clean_country}',
            )
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase1_no_address_compact",
                f'"{v_compact}" official site {clean_country}',
            )

    # Phase 2: AI Overview Natural Language Prompts
    if phase in ("phase2", "all"):
        if has_address:
            if plot_space and road_space:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase2_business_name_at_address",
                    (
                        f'What business or trade name operates under "{v_primary}" at '
                        f"{plot_space}, {road_space}, {location_phrase}?"
                    ),
                )
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase2_exec_or_owner",
                f'Who is the owner, CEO, or Managing Director of "{v_primary}" located in {location_phrase}?',
            )
            if block_hyphen and plot_hyphen and road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase2_registered_companies",
                    (
                        "What companies are registered at the address "
                        f'{block_hyphen}, {plot_hyphen}, {road_hyphen}, {location_phrase}?'
                    ),
                )
            if road_space:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase2_consumer_website",
                    (
                        f"Is there a consumer-facing website for {v_primary} "
                        f"registered at {road_space} {location_phrase}?"
                    ),
                )
        else:
            # Address not available
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase2_no_address_owner",
                f'Who is the owner, CEO, or Managing Director of "{v_primary}" in {clean_country}?',
            )
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase2_no_address_domain",
                f'What is the official website or domain of "{v_primary}" in {clean_country}?',
            )

    # Phase 3: Address-Only Pivot Discovery
    if phase in ("phase3", "all"):
        if has_address:
            if plot_hyphen and road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase3_address_pivot",
                    f'"{plot_hyphen}" "{road_hyphen}" {location_phrase} -residential',
                )
            if house_hyphen and road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase3_house_plot_variation",
                    f'"{house_hyphen}" "{road_hyphen}" {location_phrase} company OR business',
                )
            if block_hyphen and plot_hyphen and road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase3_block_plot_road",
                    f'"{block_hyphen}" "{plot_hyphen}" "{road_hyphen}" {location_phrase}',
                )
        else:
            # Address not available
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase3_no_address_registry",
                f'"{v_primary}" registry database {clean_country}',
            )

    # Phase 4: Document Hunting (Filetype/Registry Dorks)
    if phase in ("phase4", "all"):
        country_tld = _country_to_gl(clean_country)
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase4_country_registry_docs",
            f'site:.{country_tld} "{v_primary}" "{(locality or city or clean_country)}"',
        )
        if has_address:
            if plot_hyphen or road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase4_company_plot_road_docs",
                    (
                        f'filetype:pdf "{v_primary}" '
                        f'"{plot_hyphen or plot_space or plot_raw}" OR "{road_hyphen or road_space or road_raw}"'
                    ),
                )
            if plot_hyphen and road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase4_address_directory_docs",
                    f'filetype:pdf "{plot_hyphen}" "{road_hyphen}" {(locality or city or clean_country)} directory',
                )
        else:
            # Address not available
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase4_no_address_docs",
                f'filetype:pdf "{v_primary}" registry OR profile {clean_country}',
            )

    # Fallback Searches
    if phase in ("fallback", "all"):
        if clean_industry:
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "industry_fallback",
                build_industry_fallback_query(clean_company, clean_industry),
            )
        else:
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "simple_fallback",
                f"{v_primary} {clean_country}",
            )

    return attempt_queries


def build_relationship_phase_queries(
    x_name: str,
    y_name: str,
    x_domain: str,
) -> list[tuple[str, str]]:
    """Three parallel AI-Overview prose questions for one X↔Y relationship pair.

    Sent to SerpWow `engine=google` with `include_ai_overview=true`. These are PROSE
    questions (not keyword queries) because the goal is for the AI Overview to *answer*
    the relationship and type out Y's website — the typed URL is extracted from the
    overview text (and `ai_overview_sources`) as a candidate. Precision (confirmed vs
    unclear vs not_confirmed) is left to the LLM gate. X is identified by name + domain
    (the domain, from Input_URL, disambiguates); Y is used verbatim (OCR noise kept —
    Google tolerates it). Three phrasings run in parallel to raise the AI-Overview hit
    rate (the overview triggers for some wordings and not others).
    """
    x = str(x_name or "").strip()
    y = str(y_name or "").strip()
    xd = str(x_domain or "").strip()
    # Both are required: Y is the target; X is what the gate verifies the relationship
    # against — the executor short-circuits a blank X (REL_ERROR_NO_X) before the LLM.
    if not (x and y):
        return []
    x_ident = f"{x} ({xd})" if xd else x
    return [
        (
            "phase1_relationship_and_url",
            f'What is the financial relationship between {x_ident} and "{y}"? '
            f'Type out "{y}"\'s official website as a full plain-text URL starting '
            f"with https:// — do not give a hyperlink.",
        ),
        (
            "phase2_financial_evidence",
            f'Has {x_ident} invested in, funded, acquired, or backed "{y}"? '
            f'Explain the relationship, and type out "{y}"\'s official website as a '
            f"full plain-text https:// URL, not a hyperlink.",
        ),
        (
            "phase3_website_resolver",
            f'What is the official website of "{y}", the company associated with '
            f'{x_ident}? Type it out as a full plain-text https:// URL, not a hyperlink.',
        ),
    ]
