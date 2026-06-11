# backend/app/services/ai_mode/cleanup.py
"""Unified LLM cleanup: prompt from prompts/ai_cleanup.txt, results as EntityResult.
Replaces extraction.py + company_extraction.py (swapped in by a later task)."""
from __future__ import annotations

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
