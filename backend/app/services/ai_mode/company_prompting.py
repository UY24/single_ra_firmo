from __future__ import annotations

from pathlib import Path

from app.core.config import PROMPTS_DIR

from .models import CompanyEntityInput

COMPANY_SEARCH_TEMPLATE_PATH = PROMPTS_DIR / "company_search_template.txt"


def build_company_search_query(
    entities: list[CompanyEntityInput],
    prompt_path: Path = COMPANY_SEARCH_TEMPLATE_PATH,
) -> str:
    lines = []
    for idx, entity in enumerate(entities, start=1):
        lines.append(
            f'{idx}. "{entity.company_name_eng}" '
            f'(local name: "{entity.company_name_local}") '
            f"at country {entity.country_code}"
        )
    rendered = prompt_path.read_text(encoding="utf-8")
    return rendered.replace("{entities}", "\n".join(lines))
