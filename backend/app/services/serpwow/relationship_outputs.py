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

from app.services.serpwow import s3_run_store as store
from app.services.serpwow.cost import calculate_gemini_cost_usd
from app.services.serpwow.modes.relationship import (
    ai_mode_arrays,
    build_row_result,
    row_fields,
)
from app.services.serpwow.reporting import (
    retry_column,
    retry_row,
    s3_passthrough,
)

EXTRA_COLUMNS = [
    "website_url", "relationship_status",
    "relationship_summary", "relationship_evidence", "relationship_confidence",
    "website_confidence", "confidence", "flags", "attempt_log",
    "error_source", "error_reason",
]

# Column names this module writes itself, plus the "row_index" bookkeeping key every
# row carries internally (injected in s3_run_store.iter_input_rows). An input
# CSV column that happens to share one of these names must not collide with it.
_RESERVED_COLUMNS = frozenset(EXTRA_COLUMNS) | {"row_index"}

# A real input column named "row_index" has its VALUE preserved by iter_input_rows
# under this alternate key (since the "row_index" key itself is overwritten with the
# internal int index) — so the passthrough lookup must read from here, not "row_index".
_SOURCE_KEY_OVERRIDES = {"row_index": "row_index__input"}


def _passthrough_fieldnames(header: list[str]) -> list[tuple[str, str]]:
    """(output_name, source_name) for each ORIGINAL CSV column, deduped against this
    module's reserved/computed names. The rule itself is shared with gmaps and AI Mode —
    see common.text.passthrough_fieldnames."""
    return s3_passthrough(header, _RESERVED_COLUMNS)


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
        "relationship_status": result["relationship_status"],
        "relationship_summary": str(rel.get("summary") or ""),
        "relationship_evidence": "\n".join(
            str(i) for i in evidence if str(i).strip()),
        "relationship_confidence": int(rel.get("relationship_confidence_score") or 0),
        "website_confidence": int(rel.get("website_confidence_score") or 0),
        "confidence": int(parsed.get("confidence_score") or 0),
        "flags": _flags_csv(rel),
        # The row's audit trail: how many text blocks/references came back, how many
        # candidates survived filtering, and the candidate set the gate could pick from.
        "attempt_log": result.get("attempt_log", ""),
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

    # Timings come off the counters, which carry created_at from upload and the two
    # per-phase totals the runner accumulates.
    elapsed_total = counters.elapsed_seconds()
    scrape_seconds = int(counters.values.get("scrape_seconds") or 0)
    llm_seconds = int(counters.values.get("llm_seconds") or 0)

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

    # retry.csv carries the INPUT header, not the enriched one: it exists to be uploaded
    # straight back to /uploads/relationship.
    reason_column = retry_column(header)
    retry_tmp, retry_text = _temp_file()
    retry_text.write("﻿")
    retry_writer = csv.DictWriter(retry_text, fieldnames=header + [reason_column],
                                  extrasaction="ignore")
    retry_writer.writeheader()

    counts = {"confirmed": 0, "not_confirmed": 0, "unclear": 0}
    outcomes = {"found": 0, "not_found": 0, "errored": 0}
    by_source: dict[str, int] = {}
    by_category: dict[str, int] = {}
    requests = successes = credits = billed_empty = error_requests = 0
    # Rows scrape.do answered (HTTP 200, no error) where AI Mode wrote no prose. This is
    # the number that says whether the SEARCH PROMPT is working: references alone give
    # the gate URLs to pick from but nothing to verify a relationship against, so a run
    # with a high count here will read not_confirmed almost everywhere.
    no_ai_text = 0
    llm_incomplete = 0
    total_rows = found = 0
    prompt_tokens = completion_tokens = 0
    llm_usd = 0.0
    model: str | None = None

    for original in store.iter_input_rows(prefix):
        total_rows += 1
        idx = int(original["row_index"])
        fields = row_fields(original)

        envelope = store.read_row(prefix, idx)
        row_never_processed = False
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
        # Derived from the stored response, not from a flag written beside it: a billed
        # 200 that returned neither prose nor citations.
        blocks, refs = ai_mode_arrays(envelope)
        if not envelope.get("error"):
            if not blocks and not refs:
                billed_empty += 1
            if not blocks:
                no_ai_text += 1
        if envelope.get("error"):
            # Only the attempts that never got a billed 200 count as "error requests"
            # — a billed HTTP-200-with-error-body call (successful_requests=1) is a
            # business-layer failure, not a failed request, and must not inflate this
            # past scrapedo_failed_requests (requests - successes).
            error_requests += max(0, int(envelope.get("request_count") or 0) - row_successes)

        # Same rule as gmaps: only rows with nothing to show for their credits. A verdict
        # (confirmed or not) is a real answer — rerunning it re-buys it. Rows with
        # references but no prose (no_ai_text) are deliberately NOT here: the gate had
        # something to work with and produced a verdict.
        # Read BEFORE the retry rule, which needs to know whether the verdict phase ever
        # produced anything for this row. `None` (not `{}`) means no object at all: the
        # Gemini shard died and nothing was written, so the row is scraped-but-unjudged
        # and a rerun redoes the LLM alone. An object with a null `parsed` is different —
        # Gemini answered and had nothing to say, which rerunning only re-buys.
        cleaned_obj = store.get_object(store.cleaned_key(prefix, idx))
        row_unjudged = cleaned_obj is None and not envelope.get("error")
        if row_unjudged:
            llm_incomplete += 1

        retry = retry_row(
            original, header, reason_column,
            attempts=int(envelope.get("request_count") or 0),
            credits=int(envelope.get("credits") or 0),
            error=str(envelope.get("error") or ""),
            billed_empty=not envelope.get("error") and not blocks and not refs,
            llm_incomplete=row_unjudged)
        if retry:
            retry_writer.writerow(retry)

        cleaned = cleaned_obj or {}
        parsed = cleaned.get("parsed")
        # LLM accounting, aggregated per row so nothing scales with run size. The UI has
        # had Model / Input tokens / Output tokens / LLM-cost tiles all along; they were
        # blank only because this summary never fed them.
        usage = cleaned.get("usage")
        if isinstance(usage, dict):
            prompt_tokens += int(usage.get("promptTokenCount") or 0)
            completion_tokens += int(usage.get("candidatesTokenCount") or 0)
            llm_usd += calculate_gemini_cost_usd(usage)
        if model is None and cleaned.get("model"):
            model = str(cleaned["model"])
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
    # ATTEMPTS, unlike every other figure here, cannot be recovered from the objects: a
    # row that took four tries and a row that took one both leave a single response. The
    # live counter is the only place that total exists, so it wins when it is higher.
    requests = max(requests, int(counters.values.get("requests") or 0))

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
        # ONE number, because there is ONE call per row — no phases to split by. It
        # counts empty text_blocks rather than billed_empty (which needs BOTH arrays
        # empty) because that is the broader signal: references with no prose still
        # leaves the gate nothing to verify a relationship against. The stricter
        # "spent credits for literally nothing" number is cost.scrapedo_billed_empty.
        # no_ai_text: a billed 200 with citations but no prose — the gate still produced a
        # verdict, so it is final. llm_incomplete: scraped fine, never judged, retryable.
        "empty_response_breakdown": {"no_ai_text": no_ai_text,
                                     "llm_incomplete": llm_incomplete},
        "confidence_mode": "llm",
        # Hardcoded, not derived: this pipeline has no per-row LLM path at all — phase 2 is
        # always the Gemini Batch verdict pass. Leaving it unset made run_detail.js read
        # undefined and render "Batch: Off", which was simply wrong.
        "is_batch": True,
        # The run-detail Model chip and the Input/Output-token tiles read exactly these.
        "model": model,
        "token_usage": {"prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens},
        # Wall-clock, from status.json's created_at to now. Phase splits are reported
        # separately below so "how long did scrape.do take vs the LLM" is answerable.
        "processing_seconds_total": elapsed_total,
        "processing_seconds_avg": (round(elapsed_total / total_rows, 3)
                                   if elapsed_total and total_rows else None),
        "phase_seconds": {"scraping": scrape_seconds, "cleaning": llm_seconds},
        "cost": {
            # scrape.do bills CREDITS, never dollars. No serpwow_* key appears here —
            # its absence is what routes this run down the scrape.do branch in the UI.
            "scrapedo_requests": requests,
            "scrapedo_successful_requests": successes,
            "scrapedo_failed_requests": max(0, requests - successes),
            "scrapedo_error_requests": error_requests,
            "scrapedo_billed_empty": billed_empty,
            "scrapedo_credits": credits,
            "llm_usd": round(llm_usd, 6),
            # scrape.do is credits-only, so the USD total is the Gemini spend alone.
            "total_usd": round(llm_usd, 6),
        },
    }

    for name, (tmp, text) in list(tmp_files.items()) + [("retry.csv",
                                                         (retry_tmp, retry_text))]:
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
