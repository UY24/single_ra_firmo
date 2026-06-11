# backend/app/services/ai_mode/run_reporting.py
"""found.csv / notFound.csv / single merged final_report.json (spec §§5-6)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from app.models.results import EntityResult

CSV_COLUMNS = ["company_name", "company_local_name", "country", "website_url",
               "confidence", "flags", "attempt_log"]


def _row(r: EntityResult) -> dict:
    return {"company_name": r.company_name, "company_local_name": r.company_local_name or "",
            "country": r.country, "website_url": r.website_url or "",
            "confidence": r.confidence, "flags": r.flags_csv(),
            "attempt_log": r.attempt_log_csv()}


def write_outputs(run_dir: Path, results: list[EntityResult],
                  summary: dict, requests: list[dict]) -> dict[str, Path]:
    found = [r for r in results if r.website_url]
    notfound = [r for r in results if not r.website_url]

    paths: dict[str, Path] = {}
    for name, rows, extra in (("found.csv", found, []), ("notFound.csv", notfound, ["error"])):
        path = run_dir / name
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS + extra)
            writer.writeheader()
            for r in rows:
                row = _row(r)
                if extra:
                    row["error"] = r.error or ""
                writer.writerow(row)
        paths[name] = path

    report = {"summary": summary, "requests": requests,
              "entities": [r.to_report_dict() for r in results]}
    report_path = run_dir / "final_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["final_report.json"] = report_path
    return paths
