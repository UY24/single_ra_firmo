from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TypeVar

from app.core.config import PROMPTS_DIR

from .models import EntityInput

T = TypeVar("T")


def chunked(items: list[T], size: int) -> Iterable[list[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


SEARCH_QUERY_TEMPLATE_PATH = PROMPTS_DIR / "search_query_template.txt"


def build_search_query(
    entities: list[EntityInput],
    prompt_path: Path = SEARCH_QUERY_TEMPLATE_PATH,
) -> str:
    lines = []
    for idx, entity in enumerate(entities, start=1):
        lines.append(f'{idx}. "{entity.entity_name}" at location {entity.address} at country {entity.country}')
    rendered = prompt_path.read_text(encoding="utf-8")
    return (
        rendered.replace("{location_label}", "full address")
        .replace("{entities}", "\n".join(lines))
    )
