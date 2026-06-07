from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from .models import EntityCleanResult, TokenUsage

CSV_COLUMNS = ["entity_name", "location", "country", "website_url"]


def build_report_dict(
    source_batch_dir: str,
    generated_at: str,
    llm: dict[str, str],
    results: list[EntityCleanResult],
    total_input_entities: int,
    entities_without_scrape_data: int,
    llm_errors: int,
    token_usage: TokenUsage,
    time_taken_seconds: float,
) -> dict:
    websites_found = sum(1 for r in results if r.official_website)
    websites_not_found = len(results) - websites_found
    return {
        "source_batch_dir": source_batch_dir,
        "generated_at": generated_at,
        "llm": llm,
        "summary": {
            "total_input_entities": total_input_entities,
            "entities_processed": len(results),
            "entities_without_scrape_data": entities_without_scrape_data,
            "websites_found": websites_found,
            "websites_not_found": websites_not_found,
            "llm_errors": llm_errors,
            "token_usage": asdict(token_usage),
            "time_taken_seconds": time_taken_seconds,
        },
        "entities": [asdict(r) for r in results],
    }


def _csv_row(result: EntityCleanResult) -> dict[str, str]:
    return {
        "entity_name": result.entity_name,
        "location": result.location,
        "country": result.country,
        "website_url": result.official_website or "",
    }


def _write_csv(path: Path, results: list[EntityCleanResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for result in results:
            writer.writerow(_csv_row(result))


def write_outputs(batch_dir: Path, report: dict, results: list[EntityCleanResult]) -> None:
    (batch_dir / "final_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    found = [r for r in results if r.official_website]
    not_found = [r for r in results if not r.official_website]
    _write_csv(batch_dir / "found.csv", found)
    _write_csv(batch_dir / "notFound.csv", not_found)
