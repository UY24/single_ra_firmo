# backend/app/services/ai_mode/cleanup.py
"""Unified LLM cleanup: prompt from prompts/ai_cleanup.txt, results as EntityResult.
Replaces extraction.py + company_extraction.py."""
from __future__ import annotations

import json

from app.core.config import PROMPTS_DIR
from app.models.entities import Entity, format_entities_for_prompt
from app.models.results import EntityResult

_SYSTEM_PROMPT = (PROMPTS_DIR / "ai_cleanup.txt").read_text(encoding="utf-8")


def build_cleanup_messages(raw_response_text: str, entities: list[Entity] | None = None) -> list[dict]:
    user = ""
    if entities:
        user += "Input companies:\n" + format_entities_for_prompt(entities) + "\n\n"
    user += "Raw search response:\n" + raw_response_text
    return [{"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user}]


def coerce_json_array(parsed: object) -> list | None:
    """Return the results ARRAY from an already-parsed LLM response, or None.

    The cleanup prompt asks for a bare JSON array, but JSON-object response modes
    (e.g. OpenAI ``json_object``) may wrap it, so tolerate ``{"entities": [...]}``
    style wrappers and a bare single-result object.
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("entities", "results", "companies", "items"):
            value = parsed.get(key)
            if isinstance(value, list):
                return value
        if len(parsed) == 1:
            (value,) = parsed.values()
            if isinstance(value, list):
                return value
        if "sno" in parsed or "company_name" in parsed:
            return [parsed]
    return None


def parse_json_array_from_text(raw_text: str) -> list | None:
    """Parse LLM text output into the JSON results ARRAY.

    ``gemini_batch.parse_json_from_text`` only returns dicts, so the batch path
    uses this instead. Strips markdown code fences before parsing.
    """
    cleaned = (raw_text or "").strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1:]
        cleaned = cleaned.rstrip("`").strip()
    try:
        parsed = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    return coerce_json_array(parsed)


def parse_cleanup_response(parsed_json: object, entities: list[Entity]) -> list[EntityResult]:
    """Map LLM array → one EntityResult per input entity (by sno; fall back to order)."""
    by_sno: dict[int, dict] = {}
    if isinstance(parsed_json, list):
        for obj in parsed_json:
            if isinstance(obj, dict):
                try:
                    by_sno[int(obj.get("sno") or 0)] = obj
                except (TypeError, ValueError):
                    pass
    results = []
    for e in entities:
        obj = by_sno.get(e.sno)
        if obj is None:
            results.append(EntityResult(
                company_name=e.company_name, country=e.country, sno=e.sno,
                company_local_name=e.company_local_name,
                error="missing from LLM response"))
        else:
            results.append(EntityResult.from_llm_object(
                obj, fallback_country=e.country, fallback_name=e.company_name,
                fallback_local=e.company_local_name, fallback_sno=e.sno))
    return results
