# backend/app/services/ai_mode/run_reporting.py
"""found.csv / notFound.csv / single merged final_report.json (spec §§5-6).

``StreamingRunReport`` is the 1M-row-safe writer: CSV rows and outcome counters
are folded in per batch (memory stays O(one batch)), and ``final_report.json``'s
``entities`` array is capped by ``AI_MODE_REPORT_ENTITIES_MAX`` (the CSVs always
carry every row). ``write_outputs`` keeps its classic signature, reimplemented on
the streaming writer. Standalone module: imports only models + serpwow.outcomes
(never ai_mode_service, which imports us).
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Iterable

from app.models.results import EntityResult
from app.services.serpwow.outcomes import SRC_GEMINI, categorize_http_error

CSV_COLUMNS = ["company_name", "company_local_name", "country", "website_url",
               "confidence", "flags", "attempt_log"]

_ENTITIES_CAP_DEFAULT = 50_000


def _entities_cap() -> int:
    raw = os.getenv("AI_MODE_REPORT_ENTITIES_MAX")
    if raw is None or not raw.strip():
        return _ENTITIES_CAP_DEFAULT
    try:
        return int(raw.strip())
    except (ValueError, TypeError):
        return _ENTITIES_CAP_DEFAULT


def classify_one_result(r: EntityResult) -> str:
    """Classify ONE finalized entity as ``found``/``not_found``/``errored``.

    Per-entity rule shared with ``classify_ai_mode_outcomes`` (a thin aggregation
    over this): mutates an untagged errored EntityResult in place, attributing it
    to Gemini (the "missing from LLM response" case), so ``final_report.json``
    carries ``error_source``/``error_category``.
    """
    if r.website_url:
        return "found"
    if not (r.error_source or r.error):
        return "not_found"
    # Genuine error. Attribute an untagged per-entity failure to Gemini.
    if not r.error_source:
        r.error_source = SRC_GEMINI
        r.error_category = r.error_category or categorize_http_error(None, r.error or "")
    return "errored"


def _row(r: EntityResult) -> dict:
    return {"company_name": r.company_name, "company_local_name": r.company_local_name or "",
            "country": r.country, "website_url": r.website_url or "",
            "confidence": r.confidence, "flags": r.flags_csv(),
            "attempt_log": r.attempt_log_csv()}


class StreamingRunReport:
    """Incremental found.csv/notFound.csv/final_report.json writer.

    Usage: ``add_batch(request_record, entity_results)`` per scrape batch (or
    ``add_request``/``add_results`` separately), then ``close(summary)`` once.
    Counters (``counts``/``by_source``/``by_category``/``websites_found``/
    ``websites_not_found``) accumulate as results are added so the caller can
    build the terminal summary without retaining results in memory.
    """

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.counts: dict[str, int] = {"found": 0, "not_found": 0, "errored": 0}
        self.by_source: dict[str, int] = {}
        self.by_category: dict[str, int] = {}
        self.websites_found = 0
        self.websites_not_found = 0
        self._requests: list[dict] = []
        self._entities: list[dict] = []
        self._entities_cap = _entities_cap()
        self._entities_omitted = False
        self._closed = False
        self._found_fh = (run_dir / "found.csv").open("w", newline="", encoding="utf-8")
        self._found = csv.DictWriter(self._found_fh, fieldnames=CSV_COLUMNS)
        self._found.writeheader()
        self._notfound_fh = (run_dir / "notFound.csv").open("w", newline="", encoding="utf-8")
        self._notfound = csv.DictWriter(self._notfound_fh, fieldnames=CSV_COLUMNS + ["error"])
        self._notfound.writeheader()

    def add_request(self, request_record: dict) -> None:
        self._requests.append(request_record)

    def add_results(self, entity_results: Iterable[EntityResult]) -> None:
        for r in entity_results:
            bucket = classify_one_result(r)
            self.counts[bucket] += 1
            if bucket == "errored":
                self.by_source[r.error_source] = self.by_source.get(r.error_source, 0) + 1
                if r.error_category:
                    self.by_category[r.error_category] = (
                        self.by_category.get(r.error_category, 0) + 1
                    )
            if r.website_url:
                self.websites_found += 1
                self._found.writerow(_row(r))
            else:
                self.websites_not_found += 1
                row = _row(r)
                row["error"] = r.error or ""
                self._notfound.writerow(row)
            if not self._entities_omitted:
                self._entities.append(r.to_report_dict())
                if len(self._entities) > self._entities_cap:
                    # Too large for one JSON report — the CSVs carry every row.
                    self._entities = []
                    self._entities_omitted = True

    def add_batch(self, request_record: dict | None,
                  entity_results: Iterable[EntityResult]) -> None:
        if request_record is not None:
            self.add_request(request_record)
        self.add_results(entity_results)

    def close(self, summary: dict) -> dict[str, Path]:
        if self._closed:
            raise RuntimeError("StreamingRunReport already closed")
        self._closed = True
        self._found_fh.close()
        self._notfound_fh.close()

        report: dict = {"summary": summary, "requests": self._requests}
        if self._entities_omitted:
            report["entities_omitted"] = True
        else:
            report["entities"] = self._entities
        report_path = self.run_dir / "final_report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"found.csv": self.run_dir / "found.csv",
                "notFound.csv": self.run_dir / "notFound.csv",
                "final_report.json": report_path}


def write_outputs(run_dir: Path, results: list[EntityResult],
                  summary: dict, requests: list[dict]) -> dict[str, Path]:
    report = StreamingRunReport(run_dir)
    for record in requests:
        report.add_request(record)
    report.add_results(results)
    return report.close(summary)
