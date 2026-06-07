from __future__ import annotations

import csv
from pathlib import Path

from .models import CompanyEntityInput


def parse_isic(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


def load_company_entities(csv_path: Path) -> list[CompanyEntityInput]:
    """Load entities from a company-name CSV.

    Expected columns: ISIC, Country Code, Company Name ENG, Company Name Local.
    """
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        entities: list[CompanyEntityInput] = []
        for row_number, row in enumerate(reader, start=2):
            name_eng = (row.get("Company Name ENG") or "").strip()
            if not name_eng:
                continue
            name_local = (row.get("Company Name Local") or "").strip()
            entities.append(
                CompanyEntityInput(
                    company_name_eng=name_eng,
                    company_name_local=name_local or name_eng,
                    country_code=(row.get("Country Code") or "").strip(),
                    isic=parse_isic(row.get("ISIC")),
                    row_number=row_number,
                )
            )
    return entities
