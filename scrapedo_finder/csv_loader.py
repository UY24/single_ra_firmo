from __future__ import annotations

import csv
from pathlib import Path

from .models import EntityInput


def parse_firm_id(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


def load_entities(csv_path: Path) -> list[EntityInput]:
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        entities: list[EntityInput] = []
        for row_number, row in enumerate(reader, start=2):
            entity_name = (row.get("entity_name") or "").strip()
            if not entity_name:
                continue
            entities.append(
                EntityInput(
                    entity_name=entity_name,
                    country=(row.get("country") or "").strip(),
                    address=(row.get("address") or "").strip(),
                    firm_id=parse_firm_id(row.get("firm_id")),
                    row_number=row_number,
                )
            )
    return entities
