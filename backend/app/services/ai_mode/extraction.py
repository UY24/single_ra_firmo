from __future__ import annotations

from typing import Any

from .models import AttemptLogEntry, EntityCleanResult

_LIST_TYPES = {"ordered_list", "unordered_list", "list"}
_NULL_TOKENS = {"", "null", "none", "n/a", "na"}

SYSTEM_PROMPT = (
    "You clean up raw Google AI Mode OSINT research output about companies.\n"
    "You are given a numbered list of entity names and the raw research text "
    "(attempt logs and a usually-empty 'Final Answer' section).\n\n"
    "For EACH entity name given, in the SAME order, produce one cleaned record.\n"
    "Determine the official company-owned website from the attempt log. "
    "The accepted URL is usually the last attempt-log line containing 'URL: https://...'. "
    "Reject Google Maps, social networks, directories, registries, marketplaces, and database mirrors. "
    "If no official website is supported by the research, set official_website to null.\n\n"
    "Return ONLY a JSON object with this exact shape:\n"
    '{"entities": [{"entity_name": str, "short_details": str, '
    '"official_website": str|null, "found_at_attempt": int|null, '
    '"attempt_log": [{"query": str, "result": str, "url": str|null}]}]}\n'
    "short_details: one or two sentences summarizing what was found and why. "
    "found_at_attempt: the 1-based attempt number where the accepted URL appeared, else null. "
    "Do not invent websites. Do not add fields. Output valid JSON only."
)


def text_blocks_to_text(text_blocks: list[dict[str, Any]] | None) -> str:
    lines: list[str] = []
    for block in text_blocks or []:
        block_type = block.get("type")
        if block_type in _LIST_TYPES:
            for index, item in enumerate(block.get("list", []), start=1):
                snippet = item.get("snippet", "") if isinstance(item, dict) else str(item)
                if snippet:
                    lines.append(f"  {index}. {snippet}")
        else:
            snippet = block.get("snippet", "")
            if snippet:
                lines.append(snippet)
    return "\n".join(lines)


def references_to_lines(references: list[dict[str, Any]] | None) -> str:
    lines: list[str] = []
    for ref in references or []:
        if isinstance(ref, dict):
            lines.append(f"- {ref.get('title', '')} | {ref.get('link', '')}")
    return "\n".join(lines)


def build_messages(
    entity_names: list[str],
    text_blocks: list[dict[str, Any]] | None,
    references: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    numbered = "\n".join(f"{i}. {name}" for i, name in enumerate(entity_names, start=1))
    # ``references`` is intentionally NOT sent to the LLM: the accepted URLs already
    # appear inline in the attempt-log text blocks (e.g. "URL: https://..."), so the
    # separate reference links are redundant and only inflate prompt tokens. The
    # parameter is kept for signature stability with existing callers.
    user = (
        "Entities (in order):\n"
        f"{numbered}\n\n"
        "Raw research text:\n"
        f"{text_blocks_to_text(text_blocks)}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _clean_url(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NULL_TOKENS:
        return None
    return text


def _clean_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def _to_attempt_log(raw: Any) -> list[AttemptLogEntry]:
    entries: list[AttemptLogEntry] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                entries.append(
                    AttemptLogEntry(
                        query=str(item.get("query", "") or ""),
                        result=str(item.get("result", "") or ""),
                        url=_clean_url(item.get("url")),
                    )
                )
            elif item:
                entries.append(AttemptLogEntry(result=str(item)))
    return entries


def _to_result(name: str, item: dict[str, Any]) -> EntityCleanResult:
    return EntityCleanResult(
        entity_name=name,
        short_details=str(item.get("short_details", "") or ""),
        official_website=_clean_url(item.get("official_website")),
        found_at_attempt=_clean_int(item.get("found_at_attempt")),
        attempt_log=_to_attempt_log(item.get("attempt_log")),
    )


def parse_results(parsed: Any, expected_names: list[str]) -> list[EntityCleanResult]:
    raw_entities = parsed.get("entities") if isinstance(parsed, dict) else None
    raw_entities = raw_entities if isinstance(raw_entities, list) else []

    by_name: dict[str, dict[str, Any]] = {}
    for item in raw_entities:
        if isinstance(item, dict):
            name = str(item.get("entity_name", "") or "").strip()
            if name:
                by_name.setdefault(name, item)

    results: list[EntityCleanResult] = []
    for index, name in enumerate(expected_names):
        item = by_name.get(name)
        if (
            item is None
            and index < len(raw_entities)
            and isinstance(raw_entities[index], dict)
            and not str(raw_entities[index].get("entity_name", "") or "").strip()
        ):
            item = raw_entities[index]
        if item is None:
            results.append(EntityCleanResult(entity_name=name, error="missing from LLM output"))
            continue
        results.append(_to_result(name, item))
    return results
