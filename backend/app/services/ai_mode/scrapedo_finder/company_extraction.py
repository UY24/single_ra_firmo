from __future__ import annotations

from typing import Any

from .extraction import text_blocks_to_text
from .models import CompanyCleanResult, CompanyEntityInput, CompanyFlag

_NULL_TOKENS = {"", "null", "none", "n/a", "na"}

COMPANY_SYSTEM_PROMPT = (
    "You clean up raw Google AI Mode OSINT research output about companies.\n"
    "Each company was researched using only its English name, its local-language name, "
    "and a 2-letter country code (no address).\n\n"
    "You are given a numbered list of companies (English name, local name, country code) "
    "and the raw research text.\n\n"
    "For EACH company given, in the SAME order, produce one cleaned record. "
    "Determine the official company-owned website from the research. "
    "Reject Google Maps, social networks, directories, registries, marketplaces, and database mirrors. "
    "If no official website is supported by the research, set website_url to null.\n\n"
    "Return ONLY a JSON object with this exact shape:\n"
    '{"entities": [{"company_name_eng": str, "thinking": str, '
    '"confidence": int, "flags": [{"flag": str, "why": str}], '
    '"website_url": str|null}]}\n'
    "thinking: one or two sentences on what was searched and concluded. "
    "confidence: integer 0-100 reflecting HOW STRONG THE SUPPORTING DATA IS for the chosen "
    "website (more matching signals = higher; use 0 when no website is found). "
    "flags: the signals that drove the decision, each a short tag plus a one-line reason "
    "(e.g. name_match, local_name_match, tld_match, contact_page_verified for found; "
    "no_results, only_directories, only_social, parked_domain, ambiguous_name for not found). "
    "Do not invent websites. Do not add fields. Output valid JSON only."
)


def build_company_messages(
    entities: list[CompanyEntityInput],
    text_blocks: list[dict[str, Any]] | None,
    references: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    numbered = "\n".join(
        f'{i}. "{e.company_name_eng}" (local name: "{e.company_name_local}") '
        f"at country {e.country_code}"
        for i, e in enumerate(entities, start=1)
    )
    # ``references`` is intentionally NOT sent to the LLM: the accepted URLs already
    # appear inline in the attempt-log text blocks, so the separate reference links are
    # redundant and only inflate prompt tokens. Kept for signature stability.
    user = (
        "Companies (in order):\n"
        f"{numbered}\n\n"
        "Raw research text:\n"
        f"{text_blocks_to_text(text_blocks)}"
    )
    return [
        {"role": "system", "content": COMPANY_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _clean_url(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NULL_TOKENS:
        return None
    return text


def _clean_confidence(value: Any) -> int:
    try:
        number = int(float(str(value).strip().rstrip("%")))
    except (ValueError, TypeError):
        return 0
    return max(0, min(100, number))


def _to_flags(raw: Any) -> list[CompanyFlag]:
    flags: list[CompanyFlag] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                flags.append(
                    CompanyFlag(
                        flag=str(item.get("flag", "") or ""),
                        why=str(item.get("why", "") or ""),
                    )
                )
            elif item:
                flags.append(CompanyFlag(flag=str(item)))
    return flags


def _to_result(entity: CompanyEntityInput, item: dict[str, Any]) -> CompanyCleanResult:
    return CompanyCleanResult(
        company_name_eng=entity.company_name_eng,
        company_name_local=entity.company_name_local,
        country_code=entity.country_code,
        thinking=str(item.get("thinking", "") or ""),
        confidence=_clean_confidence(item.get("confidence")),
        flags=_to_flags(item.get("flags")),
        website_url=_clean_url(item.get("website_url")),
    )


def parse_company_results(
    parsed: Any, expected: list[CompanyEntityInput]
) -> list[CompanyCleanResult]:
    raw_entities = parsed.get("entities") if isinstance(parsed, dict) else None
    raw_entities = raw_entities if isinstance(raw_entities, list) else []

    by_name: dict[str, dict[str, Any]] = {}
    for item in raw_entities:
        if isinstance(item, dict):
            name = str(item.get("company_name_eng", "") or "").strip()
            if name:
                by_name.setdefault(name, item)

    results: list[CompanyCleanResult] = []
    for index, entity in enumerate(expected):
        item = by_name.get(entity.company_name_eng)
        if (
            item is None
            and index < len(raw_entities)
            and isinstance(raw_entities[index], dict)
            and not str(raw_entities[index].get("company_name_eng", "") or "").strip()
        ):
            item = raw_entities[index]
        if item is None:
            results.append(
                CompanyCleanResult(
                    company_name_eng=entity.company_name_eng,
                    company_name_local=entity.company_name_local,
                    country_code=entity.country_code,
                    error="missing from LLM output",
                )
            )
            continue
        results.append(_to_result(entity, item))
    return results
