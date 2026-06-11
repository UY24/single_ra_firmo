from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from .models import CompanyCleanResult, CompanyFlag, TokenUsage

FOUND_COLUMNS = [
    "company_name_eng",
    "company_name_local",
    "country_code",
    "website_url",
    "confidence",
    "flags",
]
NOT_FOUND_COLUMNS = [
    "company_name_eng",
    "company_name_local",
    "country_code",
    "confidence",
    "flags",
]


def build_company_report_dict(
    source_batch_dir: str,
    generated_at: str,
    llm: dict[str, str],
    results: list[CompanyCleanResult],
    total_input_entities: int,
    entities_without_scrape_data: int,
    llm_errors: int,
    token_usage: TokenUsage,
    time_taken_seconds: float,
) -> dict:
    websites_found = sum(1 for r in results if r.website_url)
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


def _flags_to_text(flags: list[CompanyFlag]) -> str:
    return "; ".join(f"{f.flag}: {f.why}".strip(": ").strip() for f in flags if f.flag or f.why)


def _found_row(result: CompanyCleanResult) -> dict[str, str]:
    return {
        "company_name_eng": result.company_name_eng,
        "company_name_local": result.company_name_local,
        "country_code": result.country_code,
        "website_url": result.website_url or "",
        "confidence": str(result.confidence),
        "flags": _flags_to_text(result.flags),
    }


def _not_found_row(result: CompanyCleanResult) -> dict[str, str]:
    return {
        "company_name_eng": result.company_name_eng,
        "company_name_local": result.company_name_local,
        "country_code": result.country_code,
        "confidence": str(result.confidence),
        "flags": _flags_to_text(result.flags),
    }


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_company_outputs(
    batch_dir: Path, report: dict, results: list[CompanyCleanResult]
) -> None:
    (batch_dir / "final_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    found = [r for r in results if r.website_url]
    not_found = [r for r in results if not r.website_url]
    _write_csv(batch_dir / "found.csv", FOUND_COLUMNS, [_found_row(r) for r in found])
    _write_csv(
        batch_dir / "notFound.csv", NOT_FOUND_COLUMNS, [_not_found_row(r) for r in not_found]
    )
