from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import BatchRunResult, JsonDict, ScrapeDoRequestRecord


def make_batch_dir(base_dir: Path, batch_id: str) -> Path:
    path = base_dir / "reports" / batch_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "raw_scrapedo_response").mkdir(parents=True, exist_ok=True)
    return path


def write_scrapedo_response(batch_dir: Path, request_index: int, payload: JsonDict) -> str:
    path = batch_dir / "raw_scrapedo_response" / f"request_{request_index:03d}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path.relative_to(batch_dir))


def save_reports(run: BatchRunResult, batch_dir: Path) -> None:
    (batch_dir / "report.json").write_text(
        json.dumps(build_report(run), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    save_request_log(run.request_records, batch_dir)


def build_report(run: BatchRunResult) -> dict:
    return {
        "batch_id": run.batch_id,
        "csv_file": run.csv_file,
        "input_type": run.input_type,
        "total_entities": run.total_entities,
        "scrapedo_request_count": run.scrapedo_request_count,
        "failed_request_count": sum(1 for record in run.request_records if record.error),
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "batch_duration_seconds": run.batch_duration_seconds,
        "requests": [asdict(record) for record in run.request_records],
    }


def save_request_log(records: list[ScrapeDoRequestRecord], batch_dir: Path) -> Path:
    path = batch_dir / "run.log"
    lines = []
    for record in records:
        lines.append(
            f"{record.request_index} status={record.status or 'unknown'} "
            f"seconds={record.time_taken_seconds:.2f} response={record.raw_json_file or 'n/a'} "
            f"entities={len(record.entity_names)}"
        )
        if record.error:
            lines.append(f"ERROR: {record.error}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path
