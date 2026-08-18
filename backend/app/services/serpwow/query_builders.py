# backend/app/services/serpwow/query_builders.py
"""Search-query construction for SerpWow modes (primary/fallback + phase queries)."""
from __future__ import annotations

import re
from typing import Any, Optional

from app.core.config import PROMPTS_DIR
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


_RELATIONSHIP_PROMPT_CACHE: Optional[str] = None


def load_relationship_prompt() -> str:
    """The single AI Mode search prompt, read once per process.

    Lives in app/prompts/ so it can be tuned without a code change — edit the file and
    restart the worker. The filename is fixed, exactly like AI Mode's own prompts in
    ``ai_mode/mode_config.py``.
    """
    global _RELATIONSHIP_PROMPT_CACHE
    if _RELATIONSHIP_PROMPT_CACHE is None:
        _RELATIONSHIP_PROMPT_CACHE = (
            PROMPTS_DIR / "relationship_search.txt"
        ).read_text(encoding="utf-8").strip()
    return _RELATIONSHIP_PROMPT_CACHE


def build_relationship_search_query(
    x_name: str,
    y_name: str,
    x_domain: str,
    input_url: str,
) -> str:
    """Fill the prompt for ONE row.

    The prompt file may use exactly these four placeholders: {x_name}, {y_name},
    {x_domain} (derived from input_url) and {input_url}. There is no location — the
    CSV carries only Company X, Company Y and the portfolio-page URL.

    Company Y is passed VERBATIM including OCR noise — that noise is meaningful input
    the model is explicitly asked to resolve, not something to clean up.
    """
    return load_relationship_prompt().format(
        x_name=(x_name or "").strip(),
        y_name=(y_name or "").strip(),
        x_domain=(x_domain or "").strip() or "an unknown domain",
        input_url=(input_url or "").strip() or "not provided",
    )
