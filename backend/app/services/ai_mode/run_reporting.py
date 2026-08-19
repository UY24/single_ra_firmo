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
import logging
import os
from pathlib import Path
from typing import Iterable, Iterator

from app.models.results import EntityResult
from app.services.common.text import passthrough_fieldnames, passthrough_row
from app.services.serpwow.outcomes import SRC_GEMINI, categorize_http_error

RESULT_COLUMNS = ["website_url", "confidence", "flags", "attempt_log"]
# Fallback header, used only when the run's input.csv can't be passed through (see
# StreamingRunReport). Mirrors reporting.CSV_COLUMNS.
CSV_COLUMNS = ["company_name", "company_local_name", "country"] + RESULT_COLUMNS
# "error" is reserved for BOTH files, not just notFound.csv, so an input column by that
# name is renamed identically in each and the two headers stay parallel.
_RESERVED = frozenset(RESULT_COLUMNS) | {"error"}

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

    def __init__(self, run_dir: Path, company_column: str | None = None):
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
        # Write to temp names and rename in close(): a crashed finish must never
        # leave a partial found.csv/notFound.csv where the files API would list
        # and serve it (and the S3 mirror would upload it) as if complete.
        self._found_tmp = run_dir / "found.csv.tmp"
        self._notfound_tmp = run_dir / "notFound.csv.tmp"
        columns = self._open_input(company_column) or CSV_COLUMNS
        # utf-8-sig: the BOM makes Excel/Numbers auto-detect UTF-8 instead of a legacy
        # 8-bit encoding, so em dashes / arrows / accents don't render as mojibake.
        # extrasaction: in passthrough mode _row()'s company_local_name is not a column.
        self._found_fh = self._found_tmp.open("w", newline="", encoding="utf-8-sig")
        self._found = csv.DictWriter(self._found_fh, fieldnames=columns,
                                     extrasaction="ignore")
        self._found.writeheader()
        self._notfound_fh = self._notfound_tmp.open("w", newline="", encoding="utf-8-sig")
        self._notfound = csv.DictWriter(self._notfound_fh, fieldnames=columns + ["error"],
                                        extrasaction="ignore")
        self._notfound.writeheader()

    def _open_input(self, company_column: str | None) -> list[str] | None:
        """Open a forward-only cursor over the run's input.csv, and return the output
        columns (its header, then the computed ones) — or None to keep CSV_COLUMNS.

        The user's own columns are what make the output line up with what they uploaded.
        Alignment is positional-by-consumption — one input row per EntityResult, which
        holds because Phase 3 adds results batch by batch in ascending request_index,
        one per input entity, failed batches included. Deliberately NOT a join on
        EntityResult.sno: that field takes the LLM's echoed value when present.

        None (headerless/positional input, or no readable input.csv) keeps the classic
        columns — which is also what makes the plain write_outputs() entry point and
        every temp-dir unit test work unchanged.
        """
        self._input_fh = None
        self._originals: Iterator[dict] = iter(())
        self._passthrough: list[tuple[str, str]] = []
        if not company_column:
            return None
        try:
            self._input_fh = (self.run_dir / "input.csv").open(
                "r", newline="", encoding="utf-8-sig", errors="replace")
        except OSError:
            return None
        reader = csv.DictReader(self._input_fh)
        self._passthrough = passthrough_fieldnames(reader.fieldnames or [], _RESERVED)
        # parse_entities_csv SKIPS a row whose company cell is empty and does NOT spend
        # an sno on it (models/entities.py) — replay that exact rule here, or every
        # column shifts down by one from the first blank-name row onwards.
        self._originals = (row for row in reader
                           if (row.get(company_column) or "").strip())
        return [out for out, _src in self._passthrough] + RESULT_COLUMNS

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
            row = _row(r)
            if self._passthrough:
                # One input row per result. Exhausted -> blank cells, never an exception
                # at the last step of an otherwise-successful 1M-row run. Applied AFTER
                # _row so an input column named company_name wins over the LLM's echo —
                # "verbatim" means the user's cell.
                row.update(passthrough_row(next(self._originals, {}),
                                           self._passthrough))
            if r.website_url:
                self.websites_found += 1
                self._found.writerow(row)
            else:
                self.websites_not_found += 1
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

    def abort(self) -> None:
        """Discard the in-progress CSVs (finish crashed) — never raises."""
        if self._closed:
            return
        self._closed = True
        self._close_input()
        for fh, tmp in ((self._found_fh, self._found_tmp),
                        (self._notfound_fh, self._notfound_tmp)):
            try:
                fh.close()
            except Exception:
                pass
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _close_input(self) -> None:
        if self._input_fh is not None:
            try:
                self._input_fh.close()
            except OSError:
                pass
            self._input_fh = None

    def close(self, summary: dict) -> dict[str, Path]:
        if self._closed:
            raise RuntimeError("StreamingRunReport already closed")
        self._closed = True
        # Leftover input rows mean the cursor and the results fell out of step, which
        # shifts every passthrough cell and has no other symptom. Warn, don't fail: the
        # CSVs are written and the run is otherwise complete.
        if self._passthrough and next(self._originals, None) is not None:
            logging.getLogger(__name__).warning(
                "input.csv has unconsumed rows — passthrough columns may be misaligned")
        self._close_input()
        self._found_fh.close()
        self._notfound_fh.close()
        os.replace(self._found_tmp, self.run_dir / "found.csv")
        os.replace(self._notfound_tmp, self.run_dir / "notFound.csv")

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
