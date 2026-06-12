# backend/app/services/ai_mode/rerun.py
"""Re-run: feed back only failed/unscraped rows; carry successes (spec §7 add-on #4)."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from app.models.entities import parse_entities_csv

CARRYOVER_FILENAME = "carryover.json"


def write_carryover(run_dir: Path, carryover: list[dict]) -> Path:
    """Persist carried-over successes for run_ai_mode_sync to merge at assemble time."""
    path = run_dir / CARRYOVER_FILENAME
    path.write_text(json.dumps(carryover, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _key(name: str, country: str) -> tuple[str, str]:
    return (name.strip().lower(), country.strip().lower())


def split_for_rerun(prev_run_dir: Path) -> tuple[str, list[dict]]:
    """Returns (retry_input_csv_text, carryover_entity_report_dicts)."""
    report = json.loads((prev_run_dir / "final_report.json").read_text(encoding="utf-8"))
    succeeded: dict[tuple[str, str], dict] = {}
    for ent in report.get("entities", []):
        if ent.get("website_url"):
            succeeded[_key(ent["company_name"], ent.get("country", ""))] = ent

    raw_input = (prev_run_dir / "input.csv").read_text(encoding="utf-8")
    parsed = parse_entities_csv(raw_input)

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["company_name", "country", "company_local_name",
                     "address", "firm_id", "industry"])
    retry_count = 0
    for e in parsed.entities:
        if _key(e.company_name, e.country) in succeeded:
            continue
        writer.writerow([e.company_name, e.country, e.company_local_name or "",
                         e.address or "", e.firm_id or "", e.industry or ""])
        retry_count += 1
    if retry_count == 0:
        raise ValueError("nothing to re-run: every row already succeeded")
    return out.getvalue(), list(succeeded.values())
