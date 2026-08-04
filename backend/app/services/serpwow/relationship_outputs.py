# backend/app/services/serpwow/relationship_outputs.py
"""Phase 3: stream cleaned/ + input.csv into the run's output files.

Memory is O(one row): each row is read, gated, written, and dropped. Nothing scales
with row count — including the CSV/log ASSEMBLY, which uses spooled temp files
(uploaded via boto3's multipart-capable upload_fileobj) rather than in-memory buffers.
A transient temp file used to build one output artifact is not a reintroduction of
local state: the store's object-presence data (raw/cleaned) is still the only source
of row truth, nothing here is read back as state.

report.json carries the SUMMARY ONLY — at 500k rows a per-row array would be
gigabytes, and the two CSVs already hold the per-row detail.

Outputs are split by RELATIONSHIP STATUS, not URL presence: everything that is not
"confirmed" (not_confirmed, unclear, and any error row) goes to notconfirmed_relation.csv.
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import shutil
import tempfile
from typing import Any

from app.services.serpwow import relationship_store as store
from app.services.serpwow.modes.relationship import build_row_result, row_fields

EXTRA_COLUMNS = [
    "website_url", "resolved_company_y_name", "relationship_status",
    "relationship_summary", "relationship_evidence", "relationship_confidence",
    "website_confidence", "confidence", "flags", "error_source", "error_reason",
]

# Column names this module writes itself, plus the "row_index" bookkeeping key every
# row carries internally (injected in relationship_store.iter_input_rows). An input
# CSV column that happens to share one of these names must not collide with it.
_RESERVED_COLUMNS = frozenset(EXTRA_COLUMNS) | {"row_index"}

# A real input column named "row_index" has its VALUE preserved by iter_input_rows
# under this alternate key (since the "row_index" key itself is overwritten with the
# internal int index) — so the passthrough lookup must read from here, not "row_index".
_SOURCE_KEY_OVERRIDES = {"row_index": "row_index__input"}


def _passthrough_fieldnames(header: list[str]) -> list[tuple[str, str]]:
    """(output_name, source_name) for each ORIGINAL CSV column, deduped against the
    reserved/computed names and against repeats within the header itself.

    Without this, an input column literally named e.g. "flags" produces a duplicate
    "flags" header, and row.update()'s computed "flags" silently overwrites the
    passthrough value in the row dict with no warning — breaking the "every original
    column passes through" contract. A collision gets an "__orig" suffix, and a further
    collision gets another one appended ("flags__orig__orig"), rather than being dropped.
    """
    seen = set(_RESERVED_COLUMNS)
    out: list[tuple[str, str]] = []
    for name in header:
        out_name = name
        while out_name in seen:
            out_name = f"{out_name}__orig"
        seen.add(out_name)
        out.append((out_name, _SOURCE_KEY_OVERRIDES.get(name, name)))
    return out


def _flags_csv(relationship: dict[str, Any]) -> str:
    flags = relationship.get("flags") or []
    return "\n".join(
        f"{f.get('flag')}: {f.get('why')}" for f in flags if isinstance(f, dict))


def _out_row(original: dict[str, str], passthrough: list[tuple[str, str]],
             result: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    rel = result["relationship"]
    evidence = rel.get("evidence") or parsed.get("relationship_evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    row = {out_name: str(original.get(src_name, "") or "")
           for out_name, src_name in passthrough}
    row.update({
        "website_url": result["official_website"],
        "resolved_company_y_name": str(rel.get("resolved_company_y_name")
                                       or parsed.get("resolved_company_y_name") or ""),
        "relationship_status": result["relationship_status"],
        "relationship_summary": str(rel.get("summary") or ""),
        "relationship_evidence": "\n".join(
            str(i) for i in evidence if str(i).strip()),
        "relationship_confidence": int(rel.get("relationship_confidence_score") or 0),
        "website_confidence": int(rel.get("website_confidence_score") or 0),
        "confidence": int(parsed.get("confidence_score") or 0),
        "flags": _flags_csv(rel),
        "error_source": result["error_source"],
        "error_reason": result["row_error"],
    })
    return row


def write_outputs(prefix: str, counters: store.Counters) -> dict[str, Any]:
    """Write the two CSVs, report.json and run.log to S3. Returns the summary."""
    counters.set_phase("reporting")
    counters.flush(force=True)

    with contextlib.ExitStack() as stack:
        return _write_outputs(prefix, counters, stack)


def _write_outputs(prefix: str, counters: store.Counters,
                    stack: contextlib.ExitStack) -> dict[str, Any]:
    def _temp_file() -> tuple[Any, io.TextIOWrapper]:
        """A binary spooled temp file wrapped for text I/O: csv.writer needs text, S3's
        upload_fileobj needs bytes. Returns (binary_file, text_wrapper) — write through
        the text wrapper, flush() it (never close() it, which would close the binary file
        too), then hand the binary file to store.put_fileobj.

        The wrapper (and, transitively, the underlying binary file) is closed when
        write_outputs returns, however it returns — otherwise every temp file leaks an
        fd (ResourceWarning) until GC."""
        tmp = tempfile.TemporaryFile()
        text = io.TextIOWrapper(tmp, encoding="utf-8", newline="")
        stack.callback(text.close)
        return tmp, text

    header = store.read_input_header(prefix)
    passthrough = _passthrough_fieldnames(header)
    fieldnames = [out_name for out_name, _src in passthrough] + EXTRA_COLUMNS

    # Spooled temp files rather than StringIO: the CSV text is built incrementally on
    # disk and the per-row objects are dropped as we go, so memory stays O(one row)
    # regardless of run size. upload_fileobj does multipart automatically.
    tmp_files = {name: _temp_file() for name in
                 ("confirmed_relation.csv", "notconfirmed_relation.csv")}
    writers = {}
    for name, (_tmp, text) in tmp_files.items():
        text.write("﻿")  # BOM: Excel reads a BOM-less UTF-8 CSV as Mac Roman
        writer = csv.DictWriter(text, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writers[name] = writer

    log_tmp, log_text = _temp_file()
    counts = {"confirmed": 0, "not_confirmed": 0, "unclear": 0}
    outcomes = {"found": 0, "not_found": 0, "errored": 0}
    by_source: dict[str, int] = {}
    by_category: dict[str, int] = {}
    requests = successes = credits = billed_empty = error_requests = 0
    total_rows = found = 0

    for original in store.iter_input_rows(prefix):
        total_rows += 1
        idx = int(original["row_index"])
        fields = row_fields(original)

        envelope = store.get_object(store.raw_key(prefix, idx))
        row_never_processed = False
        if envelope is None:
            envelope = store.get_object(store.error_key(prefix, idx))
        if envelope is None:
            # Not a scrape.do failure — this row never got a raw or error object at
            # all (crash before either write landed). Attributing it to "scrapedo"
            # would misreport an internal gap as a provider failure.
            envelope = {"error": "Row was never processed.", "error_category": "internal"}
            row_never_processed = True

        row_successes = int(envelope.get("successful_requests") or 0)
        requests += int(envelope.get("request_count") or 0)
        successes += row_successes
        credits += int(envelope.get("credits") or 0)
        if envelope.get("billed_empty"):
            billed_empty += 1
        if envelope.get("error"):
            # Only the attempts that never got a billed 200 count as "error requests"
            # — a billed HTTP-200-with-error-body call (successful_requests=1) is a
            # business-layer failure, not a failed request, and must not inflate this
            # past scrapedo_failed_requests (requests - successes).
            error_requests += max(0, int(envelope.get("request_count") or 0) - row_successes)

        cleaned = store.get_object(store.cleaned_key(prefix, idx)) or {}
        parsed = cleaned.get("parsed")
        result = build_row_result(
            fields, envelope, parsed if isinstance(parsed, dict) else None,
            cleaned.get("candidates") or [], str(cleaned.get("x_domain") or ""))

        status = result["relationship_status"]
        counts[status] = counts.get(status, 0) + 1
        if envelope.get("error"):
            outcomes["errored"] += 1
            source = "internal" if row_never_processed else "scrapedo"
            by_source[source] = by_source.get(source, 0) + 1
            category = str(envelope.get("error_category") or "unknown")
            by_category[category] = by_category.get(category, 0) + 1
        elif result["official_website"]:
            outcomes["found"] += 1
            found += 1
        else:
            outcomes["not_found"] += 1

        name = ("confirmed_relation.csv" if status == "confirmed"
                else "notconfirmed_relation.csv")
        writers[name].writerow(
            _out_row(original, passthrough, result,
                     parsed if isinstance(parsed, dict) else {}))

        log_text.write(
            f"[{idx}] {fields['y_name']} -> "
            f"{result['official_website'] or 'not found'} ({status})"
            + (f" — {result['row_error']}" if result["row_error"] else "")
            + "\n")

    # task_errors counts row/shard tasks that RAISED (relationship_runner._drain). Those
    # leave no per-row error marker — a verdict shard that died takes its rows' verdicts
    # with it and they read as plain "unclear"/llm_missing — so without this a run whose
    # whole already-paid-for shard was discarded reported a clean "completed".
    task_errors = int(counters.values.get("task_errors") or 0)
    run_status = ("completed_with_errors" if outcomes["errored"] or task_errors
                  else "completed")
    # A stop can land mid-verdict or mid-reporting, and any Gemini batch shard already
    # submitted by that point still runs to completion and gets billed regardless of this
    # check (run_verdict_phase stops SUBMITTING new shards, it cannot un-buy live ones).
    # All this does is keep a deliberately-halted run from being mislabeled
    # "completed_with_errors"; every row processed up to the stop is written out normally.
    if store.stop_requested(prefix):
        run_status = "stopped"

    summary = {
        "pipeline": "relationship",
        "status": run_status,
        "total_rows": total_rows,
        "websites_found": found,
        "websites_not_found": total_rows - found,
        "relationship_breakdown": counts,
        "outcome_breakdown": outcomes,
        "error_breakdown": {"by_source": by_source, "by_category": by_category,
                            # Why the run can be completed_with_errors while every
                            # by_source count is 0: a task that raised has no error marker.
                            "task_errors": task_errors},
        "empty_response_breakdown": {"empty": billed_empty},
        "confidence_mode": "llm",
        # Hardcoded, not derived: this pipeline has no per-row LLM path at all — phase 2 is
        # always the Gemini Batch verdict pass. Leaving it unset made run_detail.js read
        # undefined and render "Batch: Off", which was simply wrong.
        "is_batch": True,
        "cost": {
            # scrape.do bills CREDITS, never dollars. No serpwow_* key appears here —
            # its absence is what routes this run down the scrape.do branch in the UI.
            "scrapedo_requests": requests,
            "scrapedo_successful_requests": successes,
            "scrapedo_failed_requests": max(0, requests - successes),
            "scrapedo_error_requests": error_requests,
            "scrapedo_billed_empty": billed_empty,
            "scrapedo_credits": credits,
            "llm_usd": 0.0,
            "total_usd": 0.0,
        },
    }

    for name, (tmp, text) in tmp_files.items():
        text.flush()
        store.put_fileobj(f"{prefix}/{name}", tmp, content_type="text/csv")
    store.put_bytes(
        f"{prefix}/report.json",
        json.dumps({"summary": summary}, ensure_ascii=False, indent=2).encode("utf-8"),
        content_type="application/json")

    # run.log's header needs the run-wide totals, which are only known once every row
    # has streamed through — but the header must appear FIRST in the file. Write it to
    # its own temp file, then copy the already-written per-row body after it (bounded
    # chunked copy, not a read-back-into-memory) rather than buffering all log lines.
    log_text.flush()
    final_tmp, final_text = _temp_file()
    final_text.write("\n".join([
        f"# relationship run — status={run_status}",
        f"# rows={total_rows} found={found} not_found={total_rows - found}",
        (f"# cost: scrapedo_requests={requests} (ok={successes} "
         f"errors={error_requests}) scrapedo_credits={credits} "
         f"scrapedo_billed_empty={billed_empty}"),
        "",
    ]) + "\n")
    final_text.flush()
    log_tmp.seek(0)
    shutil.copyfileobj(log_tmp, final_tmp)
    store.put_fileobj(f"{prefix}/run.log", final_tmp, content_type="text/plain")

    # phase (scan-eligibility) only ever lands on "completed" or "stopped" — never
    # "completed_with_errors", which is a report/log label, not a phase value. Both
    # "completed" and "stopped" are terminal in relationship_runner._TERMINAL_PHASES,
    # so a redrive never re-touches a finished-or-stopped run either way.
    counters.set_phase("stopped" if run_status == "stopped" else "completed")
    counters.flush(force=True)
    return summary
