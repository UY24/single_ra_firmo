# backend/app/services/serpwow/gmaps_outputs.py
"""Phase 2: stream rows/ + input.csv into found.csv, notFound.csv, report.json, run.log.

Memory is O(one row): each row is read, converted, written and dropped, and the CSV/log
assembly uses spooled temp files rather than in-memory buffers. Nothing here scales with
row count — which is the whole point of the move off state.json, where the old reporting
path materialised every EntityResult and embedded every row in report.json.

The per-row conversion is the SHARED reporting.row_to_entity_result, unchanged:
this module streams the same rows the old path held in a list, so found.csv and
notFound.csv keep byte-identical columns.
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
from app.services.serpwow.modes.gmaps import row_fields
from app.services.common.text import passthrough_row
from app.services.serpwow.reporting import (
    RESULT_COLUMNS,
    _build_cost,
    _cost_log_line,
    _derive_outcome,
    result_cells,
    retry_column,
    retry_row,
    row_to_entity_result,
    s3_passthrough,
)


def _state_row(idx: int, fields: dict[str, Any],
               stored: dict[str, Any] | None) -> dict[str, Any]:
    """The row shape reporting expects, rebuilt from one rows/ object.

    row_to_entity_result and _derive_outcome read a state row — {result: {context, ...},
    company_name, country, outcome, ...}. Rebuilding it here is what lets both stay
    untouched, so the two pipelines cannot drift in what a "found" row means.
    """
    if stored is None:
        # No object at all: the row was never processed (a crash before either write
        # landed). Attributing it to scrape.do would misreport an internal gap as a
        # provider failure.
        return {"row_index": idx, "company_name": fields.get("company_name") or "",
                "country": fields.get("country") or "", "status": "failed",
                "outcome": "error", "error_source": "internal",
                "error_category": "internal", "error": "Row was never processed.",
                "result": {}}

    context = stored.get("context") or {}
    error = context.get("error")
    row_error = stored.get("row_error") or context.get("row_error") or ""
    result = {
        "official_website": stored.get("official_website") or None,
        "summary": stored.get("summary") or "",
        "context": context,
        "gemini_cost_usd": 0.0,
    }
    row: dict[str, Any] = {
        "row_index": idx,
        "company_name": fields.get("company_name") or "",
        "country": fields.get("country") or "",
        "firm_id": fields.get("firm_id") or "",
        "status": "failed" if error else "completed",
        "result": result,
    }
    if error:
        row.update(outcome="error", error_source="scrapedo", error=str(error),
                   error_category=context.get("error_category") or "unknown")
    elif row_error:
        row.update(outcome="not_found", error=row_error)
    return row


def write_outputs(prefix: str, counters: store.Counters) -> dict[str, Any]:
    """Write both CSVs, report.json and run.log to S3. Returns the summary."""
    counters.set_phase("reporting")
    counters.flush(force=True)
    with contextlib.ExitStack() as stack:
        return _write_outputs(prefix, counters, stack)


def _write_outputs(prefix: str, counters: store.Counters,
                   stack: contextlib.ExitStack) -> dict[str, Any]:
    def _temp_file() -> tuple[Any, io.TextIOWrapper]:
        """A binary spooled temp file wrapped for text I/O: csv.writer needs text, S3's
        upload_fileobj needs bytes. Closed when write_outputs returns, however it returns,
        or every temp file leaks an fd until GC."""
        tmp = tempfile.TemporaryFile()
        text = io.TextIOWrapper(tmp, encoding="utf-8", newline="")
        stack.callback(text.close)
        return tmp, text

    elapsed_total = counters.elapsed_seconds()

    # Every output here is the USER'S FILE plus what we worked out: the input header goes
    # out verbatim and in order, then RESULT_COLUMNS. "error" is reserved for both files
    # so an input column named "error" is renamed the same way in each.
    header = store.read_input_header(prefix)
    passthrough = s3_passthrough(header, set(RESULT_COLUMNS) | {"error"})
    input_columns = [out for out, _src in passthrough]

    files = {name: _temp_file() for name in ("found.csv", "notFound.csv")}
    writers = {}
    for name, (_tmp, text) in files.items():
        text.write("﻿")  # BOM: Excel reads a BOM-less UTF-8 CSV as Mac Roman
        extra = ["error"] if name == "notFound.csv" else []
        writer = csv.DictWriter(text,
                                fieldnames=input_columns + RESULT_COLUMNS + extra,
                                extrasaction="ignore")
        writer.writeheader()
        writers[name] = writer

    log_tmp, log_text = _temp_file()

    # retry.csv carries the input header ALONE (plus its one reason column): it exists to
    # be uploaded straight back to /uploads/gmaps, so our columns have no business in it.
    reason_column = retry_column(header)
    retry_tmp, retry_text = _temp_file()
    retry_text.write("﻿")
    retry_writer = csv.DictWriter(retry_text, fieldnames=header + [reason_column],
                                  extrasaction="ignore")
    retry_writer.writeheader()

    total_rows = found = 0
    outcomes = {"found": 0, "not_found": 0, "errored": 0}
    by_source: dict[str, int] = {}
    by_category: dict[str, int] = {}
    requests = successes = failed = credits = 0
    billed_empty = no_results = recovered = error_requests = billed_errors = 0

    for original in store.iter_input_rows(prefix):
        total_rows += 1
        idx = int(original["row_index"])
        fields = row_fields(original)
        stored = store.get_object(store.row_key(prefix, idx))
        if stored is None:
            # A terminal scrape failure lands in errors/, which carries the same shape
            # minus a context — read it so the row reports the provider's own message.
            marker = store.get_object(store.error_key(prefix, idx))
            if marker is not None:
                stored = {"row_index": idx, "fields": fields,
                          "context": {"error": marker.get("error"),
                                      "error_category": marker.get("error_category"),
                                      "cost_breakdown": marker.get("cost_breakdown") or {}}}
        row = _state_row(idx, fields, stored)
        result = row["result"] or {}
        cost = (result.get("context") or {}).get("cost_breakdown") or {}
        requests += int(cost.get("scrapedo_requests") or 0)
        successes += int(cost.get("scrapedo_successful_requests") or 0)
        failed += int(cost.get("scrapedo_failed_requests") or 0)
        credits += int(cost.get("scrapedo_credits") or 0)
        billed_empty += int(cost.get("scrapedo_billed_empty") or 0)
        no_results += int(cost.get("scrapedo_no_results") or 0)
        recovered += int(cost.get("scrapedo_recovered_requests") or 0)
        error_requests += int(cost.get("scrapedo_error_requests") or 0)

        entity = row_to_entity_result(row, total_rows)
        outcome = _derive_outcome(row)
        # `error` only when the row actually errored: a no-listing row and a
        # "Row has no company name." row both carry row["error"] as their display text,
        # and neither is a provider failure. The nameless one is not rerunnable at all.
        retry = retry_row(
            original, header, reason_column,
            attempts=int(cost.get("scrapedo_requests") or 0),
            credits=int(cost.get("scrapedo_credits") or 0),
            error=str(row.get("error") or "") if outcome == "error" else "",
            billed_empty=bool(cost.get("scrapedo_billed_empty")),
            no_listing=bool(cost.get("scrapedo_no_results")))
        if retry:
            retry_writer.writerow(retry)
        if outcome == "error":
            outcomes["errored"] += 1
            # A row that died WITH a billed 200 (error body) is a paid-for-nothing row,
            # not one of the free 502s. The UI splits the two on exactly this number.
            if int(cost.get("scrapedo_successful_requests") or 0):
                billed_errors += 1
            source = str(row.get("error_source") or "scrapedo")
            by_source[source] = by_source.get(source, 0) + 1
            category = str(row.get("error_category") or "unknown")
            by_category[category] = by_category.get(category, 0) + 1
        elif entity.website_url:
            outcomes["found"] += 1
        else:
            outcomes["not_found"] += 1

        name = "found.csv" if entity.website_url else "notFound.csv"
        csv_row = {**passthrough_row(original, passthrough), **result_cells(entity)}
        if name == "notFound.csv":
            csv_row["error"] = entity.error or ""
        writers[name].writerow(csv_row)
        if entity.website_url:
            found += 1
            log_text.write(f"[{total_rows}] {entity.company_name} ({entity.country}) -> "
                           f"{entity.website_url} (confidence={entity.confidence})\n")
        else:
            tail = f" — {entity.error}" if entity.error else ""
            log_text.write(f"[{total_rows}] {entity.company_name} ({entity.country}) -> "
                           f"not found{tail}\n")

    # ATTEMPTS cannot be recovered from the objects alone if a row's result write failed,
    # so the live counter wins when it is higher — same rule as relationship.
    requests = max(requests, int(counters.values.get("requests") or 0))
    task_errors = int(counters.values.get("task_errors") or 0)
    run_status = ("completed_with_errors" if outcomes["errored"] or task_errors
                  else "completed")
    if store.stop_requested(prefix):
        run_status = "stopped"

    summary = {
        "pipeline": "gmaps",
        "status": run_status,
        "total_rows": total_rows,
        "websites_found": found,
        "websites_not_found": total_rows - found,
        "outcome_breakdown": outcomes,
        "error_breakdown": {"by_source": by_source, "by_category": by_category,
                            "task_errors": task_errors},
        # Heuristic scoring only — the LLM confidence modes went with the state.json
        # engine, so there is no model and no token usage to report.
        "confidence_mode": "heuristic",
        "is_batch": False,
        "model": None,
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "processing_seconds_total": elapsed_total,
        "processing_seconds_avg": (round(elapsed_total / total_rows, 3)
                                   if elapsed_total and total_rows else None),
        "phase_seconds": {"scraping": int(counters.values.get("scrape_seconds") or 0)},
        # Rows where Google simply has no Maps listing: expected, unbilled, and NOT
        # errors — the number that says whether the input list is the problem.
        "empty_response_breakdown": {"no_listing": no_results,
                                     "billed_empty": billed_empty},
        "cost": _build_cost(0.0, 0, 0, requests, credits, successes, failed,
                            billed_empty, no_results, recovered, error_requests,
                            scrapedo_billed_errors=billed_errors),
    }

    for name, (tmp, text) in list(files.items()) + [("retry.csv",
                                                     (retry_tmp, retry_text))]:
        text.flush()
        store.put_fileobj(f"{prefix}/{name}", tmp, content_type="text/csv")
    store.put_bytes(
        f"{prefix}/report.json",
        json.dumps({"summary": summary}, ensure_ascii=False, indent=2).encode("utf-8"),
        content_type="application/json")

    # run.log's header needs run-wide totals, which are only known once every row has
    # streamed through — but it must appear FIRST. Write it to its own temp file, then
    # copy the already-written body after it (bounded chunked copy, not a read-back).
    log_text.flush()
    final_tmp, final_text = _temp_file()
    final_text.write("\n".join([
        f"# gmaps run — status={run_status}",
        f"# rows={total_rows} found={found} not_found={total_rows - found}",
        _cost_log_line(summary),
        "",
    ]) + "\n")
    final_text.flush()
    log_tmp.seek(0)
    shutil.copyfileobj(log_tmp, final_tmp)
    store.put_fileobj(f"{prefix}/run.log", final_tmp, content_type="text/plain")

    counters.set_phase("stopped" if run_status == "stopped" else "completed")
    counters.flush(force=True)
    return summary
