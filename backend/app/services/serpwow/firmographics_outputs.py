# backend/app/services/serpwow/firmographics_outputs.py
"""Phase 3: stream rows/ + cleaned/ + input.csv into the run's output files.

Memory is O(one row): each row is read, converted, written and dropped, and the CSV/log
assembly uses spooled temp files rather than in-memory buffers. Nothing here scales with
row count, which is the point of the move off state.json — the old path materialised every
row and embedded them all in output.json.

Column contract matches gmaps and relationship: the uploaded input header goes out verbatim
and in order, then this pipeline's computed columns. Two files, not found/notFound: this
pipeline is HANDED the website, so a found/not-found split would publish a discovery result
it never computed. The split that matters is "did we enrich it or not".
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import shutil
import tempfile
from typing import Any

from app.services.common.text import passthrough_row
from app.services.serpwow import s3_run_store as store
from app.services.serpwow.modes.firmographics import row_fields
from app.services.serpwow.reporting import (
    _build_cost,
    retry_column,
    retry_row,
    s3_passthrough,
)

# What this pipeline works out per row, appended after the uploaded header verbatim.
#
# Deliberately per-ROW only. The old 37-column export also repeated nine RUN-level values
# (upload_id, file_status, created_at, total_rows, processed_rows, success_rows,
# failed_rows, batch_status, updated_at) on every single row — those live in report.json,
# where they are stated once. Its six input_* columns are replaced by the uploaded header
# itself, which is strictly more: every column the user sent, under their own names.
# Its confidence/description/massive_proxy columns were always blank for this pipeline.
#
# `enrichment_note` is empty on an enriched row by design — there is nothing to explain
# when it worked. It carries the reason in notEnriched.csv, which is the file it is for.
RESULT_COLUMNS = ["row_index", "outcome",
                  "address", "phone", "email", "industry", "products", "services",
                  "summary", "enrichment_note",
                  "gemini_cost_usd", "total_cost_usd", "processing_seconds",
                  "raw_response_s3_key"]

_FIELD_KEYS = ("address", "phone", "email", "industry", "products", "services")


def _joined(value: Any) -> str:
    """Lists become one cell; scalars pass through. Mirrors the old XLSX export."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v)
    return "" if value is None else str(value)


def _row_result(prefix: str, idx: int) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(the rows/ object, the six fields) for one row.

    The fields come from cleaned/ when a Gemini BATCH produced them and from rows/ when the
    inline LLM did — one `or`, which is what lets inline mode skip a whole PUT per row.
    """
    stored = store.get_object(store.row_key(prefix, idx))
    if stored is None:
        return None, {}
    result = stored.get("result")
    if not isinstance(result, dict) or not any(result.get(k) for k in _FIELD_KEYS):
        cleaned = store.get_object(store.cleaned_key(prefix, idx))
        if cleaned is not None:
            # Attached whenever the object EXISTS, not only when it carries fields: its
            # presence is how the caller tells "Gemini answered nothing for this row"
            # (final — rerunning re-buys the same silence) from "the shard never returned"
            # (retryable). It also carries usage/model even on an empty answer.
            stored["_cleaned"] = cleaned
            if isinstance(cleaned.get("result"), dict):
                result = cleaned["result"]
    return stored, result if isinstance(result, dict) else {}


def write_outputs(prefix: str, counters: store.Counters) -> dict[str, Any]:
    """Write both CSVs, report.json, retry.csv and run.log to S3. Returns the summary."""
    counters.set_phase("reporting")
    counters.flush(force=True)
    with contextlib.ExitStack() as stack:
        return _write_outputs(prefix, counters, stack)


def _write_outputs(prefix: str, counters: store.Counters,
                   stack: contextlib.ExitStack) -> dict[str, Any]:
    def _temp_file() -> tuple[Any, io.TextIOWrapper]:
        """Binary spooled temp file wrapped for text I/O: csv.writer needs text, S3's
        upload_fileobj needs bytes. Closed however this returns, or every file leaks an fd."""
        tmp = tempfile.TemporaryFile()
        text = io.TextIOWrapper(tmp, encoding="utf-8", newline="")
        stack.callback(text.close)
        return tmp, text

    elapsed_total = counters.elapsed_seconds()
    header = store.read_input_header(prefix)
    passthrough = s3_passthrough(header, set(RESULT_COLUMNS))
    input_columns = [out for out, _src in passthrough]

    files = {name: _temp_file() for name in ("enriched.csv", "notEnriched.csv")}
    writers = {}
    for name, (_tmp, text) in files.items():
        text.write("﻿")  # BOM: Excel reads a BOM-less UTF-8 CSV as the OS codepage
        writer = csv.DictWriter(text, fieldnames=input_columns + RESULT_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        writers[name] = writer

    log_tmp, log_text = _temp_file()

    # retry.csv carries the input header ALONE plus one reason column: it exists to be
    # uploaded straight back to /uploads/firmographics, so our columns have no place in it.
    # Column name and membership rule both come from reporting, shared with gmaps and
    # relationship, so the three runs' rerun lists speak one vocabulary.
    reason_column = retry_column(header)
    retry_tmp, retry_text = _temp_file()
    retry_text.write("﻿")
    retry_writer = csv.DictWriter(retry_text, fieldnames=header + [reason_column],
                                  extrasaction="ignore")
    retry_writer.writeheader()

    total_rows = enriched = 0
    outcomes = {"found": 0, "not_found": 0, "errored": 0}
    by_source: dict[str, int] = {}
    by_category: dict[str, int] = {}
    requests = successes = failed_requests = credits = 0
    search_requests = search_ok = aio_requests = aio_ok = deferred = billed_empty = 0
    error_requests = billed_errors = 0
    prompt_tokens = completion_tokens = 0
    llm_usd = 0.0
    model: str | None = None
    no_overview = never_processed = no_website = llm_incomplete = 0

    for original in store.iter_input_rows(prefix):
        total_rows += 1
        idx = int(original["row_index"])
        fields = row_fields(original)
        stored, result = _row_result(prefix, idx)

        note = ""
        outcome = "not_found"
        if stored is None:
            # No object at all — a crash before either write landed. An internal gap, NOT a
            # provider failure, so it must not be attributed to scrape.do.
            marker = store.get_object(store.error_key(prefix, idx))
            note = str((marker or {}).get("error") or "Row was never processed.")
            outcome = "errored"
            never_processed += 1
            by_source["internal"] = by_source.get("internal", 0) + 1
            by_category["internal"] = by_category.get("internal", 0) + 1
        else:
            context = stored.get("context") or {}
            cost = context.get("cost_breakdown") or {}
            requests += int(cost.get("scrapedo_requests") or 0)
            successes += int(cost.get("scrapedo_successful_requests") or 0)
            failed_requests += int(cost.get("scrapedo_failed_requests") or 0)
            credits += int(cost.get("scrapedo_credits") or 0)
            search_requests += int(cost.get("scrapedo_search_requests") or 0)
            search_ok += int(cost.get("scrapedo_search_successful") or 0)
            aio_requests += int(cost.get("scrapedo_ai_overview_requests") or 0)
            aio_ok += int(cost.get("scrapedo_ai_overview_successful") or 0)
            deferred += int(cost.get("scrapedo_ai_overview_deferred") or 0)
            billed_empty += int(cost.get("scrapedo_billed_empty") or 0)
            error_requests += int(cost.get("scrapedo_error_requests") or 0)
            billed_errors += int(cost.get("scrapedo_billed_errors") or 0)
            llm_usd += float(stored.get("gemini_cost_usd") or 0.0)

            # Token usage / model: inline rows carry them under context.mapping_ai, batched
            # rows under the cleaned object. Both feed the same UI tiles.
            for usage_holder in (context.get("mapping_ai"), stored.get("_cleaned")):
                if not isinstance(usage_holder, dict):
                    continue
                usage = usage_holder.get("usage")
                if isinstance(usage, dict):
                    prompt_tokens += int(usage.get("promptTokenCount", 0) or 0)
                    completion_tokens += int(usage.get("candidatesTokenCount", 0) or 0)
                if model is None and usage_holder.get("model"):
                    model = str(usage_holder["model"])

            if context.get("formatted_results"):
                phase = (context["formatted_results"] or [{}])[0]
                note = str(phase.get("error") or "Search provider failed.")
                outcome = "errored"
                source = str(phase.get("error_source") or "scrapedo")
                by_source[source] = by_source.get(source, 0) + 1
                category = str(phase.get("error_category") or "unknown")
                by_category[category] = by_category.get(category, 0) + 1
            elif any(result.get(k) for k in _FIELD_KEYS):
                outcome = "found"
            elif stored.get("awaiting_batch") and stored.get("_cleaned") is None:
                # Scraped, billed, an AI overview WAS captured (that is why it was
                # deferred) — and the Gemini shard never came back. Its own note and its
                # own bucket: rolling it into no_ai_overview both overstated "Google gave
                # us nothing" and hid the one outcome here that a rerun actually fixes.
                note = ("LLM never completed — rerun to retry "
                        "(no scrape.do re-spend).")
                llm_incomplete += 1
            else:
                note = str(stored.get("row_error")
                           or context.get("row_error")
                           or "No firmographics extracted.")
                if stored.get("row_error"):
                    no_website += 1
                else:
                    no_overview += 1

        outcomes[outcome] += 1
        context_now = (stored or {}).get("context") or {}
        cost_now = context_now.get("cost_breakdown") or {}
        timing = context_now.get("timing") if isinstance(
            context_now.get("timing"), dict) else {}
        # Per-row wall clock, split as the row itself recorded it. Summed rather than taking
        # total_seconds, which the old state-driven worker stamped and this pipeline has no
        # equivalent of — there is no per-row task wrapper here.
        seconds = round(float(timing.get("search_seconds") or 0.0)
                        + float(timing.get("llm_seconds") or 0.0), 3)
        row_gemini_usd = float((stored or {}).get("gemini_cost_usd") or 0.0)
        csv_row = {
            **passthrough_row(original, passthrough),
            "row_index": idx,
            # found / not_found / errored — the repo's own vocabulary, and the same value
            # report.json counts in outcome_breakdown, so the two always reconcile.
            "outcome": outcome,
            **{key: _joined(result.get(key)) for key in _FIELD_KEYS},
            "summary": str((stored or {}).get("summary") or ""),
            "enrichment_note": note,
            "gemini_cost_usd": f"{row_gemini_usd:.8f}" if row_gemini_usd else "",
            # Credits are not dollars, so a firmographics row's total USD IS its LLM cost.
            "total_cost_usd": f"{row_gemini_usd:.8f}" if row_gemini_usd else "",
            "processing_seconds": seconds or "",
            # Where this row's provider SERP is. Our computed result for the same row sits
            # at the matching rows/ path, so one column locates both.
            #
            # Emitted only when a raw/ object can actually exist: `context` is None for a
            # row with no website (no call was ever made) and formatted_results means the
            # call died before returning a body. Pointing at a key that isn't there is
            # worse than an empty cell — it sends a reader looking for a missing object.
            "raw_response_s3_key": (
                store.raw_key(prefix, idx)
                if context_now and not context_now.get("formatted_results") else ""),
        }
        name = "enriched.csv" if outcome == "found" else "notEnriched.csv"
        writers[name].writerow(csv_row)
        if outcome == "found":
            enriched += 1

        # retry.csv: only rows with nothing to show for themselves. retry_row returns None
        # for a row that got a real answer, and for one with no website at all (no reason
        # flag applies) — that one is not rerunnable, since the INPUT is what is missing.
        retry = retry_row(
            original, header, reason_column,
            attempts=int(cost_now.get("scrapedo_requests") or 0),
            credits=int(cost_now.get("scrapedo_credits") or 0),
            error=note if outcome == "errored" else "",
            billed_empty=(outcome == "not_found"
                          and bool(cost_now.get("scrapedo_billed_empty"))),
            # The scrape is already paid for and already in S3, so this row's rerun costs
            # Gemini tokens and nothing else.
            llm_incomplete=bool(stored and stored.get("awaiting_batch")
                                and stored.get("_cleaned") is None))
        if retry:
            retry_writer.writerow(retry)

        log_text.write(
            f"[{total_rows}] {fields['official_website'] or '(no website)'} -> "
            + (f"enriched ({result.get('industry') or 'no industry'})" if outcome == "found"
               else f"{outcome}{' — ' + note if note else ''}") + "\n")

    # ATTEMPTS cannot be recovered from the objects if a row's result write failed, so the
    # live counter wins when higher — same rule as gmaps and relationship.
    requests = max(requests, int(counters.values.get("requests") or 0))
    credits = max(credits, int(counters.values.get("credits") or 0))
    task_errors = int(counters.values.get("task_errors") or 0)
    run_status = ("completed_with_errors" if outcomes["errored"] or task_errors
                  else "completed")
    if store.stop_requested(prefix):
        run_status = "stopped"

    scrape_seconds = int(counters.values.get("scrape_seconds") or 0)
    llm_seconds = int(counters.values.get("llm_seconds") or 0)
    summary = {
        "pipeline": "firmographics",
        "status": run_status,
        "total_rows": total_rows,
        # This pipeline enriches; it does not discover. The UI's primary tile reads
        # websites_found, so it carries the enriched count — the honest equivalent.
        "websites_found": enriched,
        "websites_not_found": total_rows - enriched,
        "outcome_breakdown": outcomes,
        "error_breakdown": {"by_source": by_source, "by_category": by_category,
                            "task_errors": task_errors},
        # No confidence concept: the website is an input, so there is nothing to be
        # confident about. None makes the UI skip the chip rather than show a meaningless one.
        "confidence_mode": None,
        # Set below, from what the OBJECTS show rather than from current env: a run that
        # batched last week must still report "batch" after the toggle is flipped.
        "is_batch": False,
        "llm_mode": None,
        "model": model,
        "token_usage": {"prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens},
        "processing_seconds_total": elapsed_total,
        "processing_seconds_avg": (round(elapsed_total / total_rows, 3)
                                   if elapsed_total and total_rows else None),
        "phase_seconds": {"scraping": scrape_seconds, "cleaning": llm_seconds},
        # Rows the provider answered with nothing usable, split by why.
        "empty_response_breakdown": {"no_ai_overview": no_overview,
                                     "deferred": deferred,
                                     "no_website": no_website,
                                     # Had an overview; the Gemini shard never delivered.
                                     # The ONLY bucket here a rerun can fix.
                                     "llm_incomplete": llm_incomplete,
                                     "never_processed": never_processed},
        "cost": _build_cost(
            llm_usd, 0, 0, requests, credits, successes, failed_requests,
            billed_empty, 0, 0, error_requests,
            scrapedo_billed_errors=billed_errors,
            scrapedo_search_requests=search_requests,
            scrapedo_search_successful=search_ok,
            scrapedo_ai_overview_requests=aio_requests,
            scrapedo_ai_overview_successful=aio_ok,
            scrapedo_ai_overview_deferred=deferred),
    }
    # llm_mode from what the objects show, not from current env: a run batched last week
    # must still report "batch" after the toggle is flipped.
    summary["is_batch"] = bool(counters.values.get("rows_cleaned"))
    summary["llm_mode"] = ("batch" if summary["is_batch"] else "inline") if model else None

    for name, (tmp, text) in list(files.items()) + [("retry.csv", (retry_tmp, retry_text))]:
        text.flush()
        store.put_fileobj(f"{prefix}/{name}", tmp, content_type="text/csv")
    store.put_bytes(
        f"{prefix}/report.json",
        json.dumps({"summary": summary}, ensure_ascii=False, indent=2).encode("utf-8"),
        content_type="application/json")

    # run.log's header needs run-wide totals, known only once every row has streamed — but
    # it must appear FIRST. Write it to its own temp file, then copy the body after it.
    log_text.flush()
    final_tmp, final_text = _temp_file()
    final_text.write("\n".join([
        f"# firmographics run — status={run_status}",
        f"# rows={total_rows} enriched={enriched} not_enriched={total_rows - enriched}",
        f"# scrape.do: {requests} attempts, {credits} credits "
        f"({search_ok} searches x10 + {aio_ok} ai-overview x5)",
        f"# llm: model={model or '-'} mode={summary['llm_mode'] or '-'} "
        f"tokens={summary['token_usage']['total_tokens']} usd={llm_usd:.6f}",
        "",
    ]) + "\n")
    final_text.flush()
    log_tmp.seek(0)
    shutil.copyfileobj(log_tmp, final_tmp)
    store.put_fileobj(f"{prefix}/run.log", final_tmp, content_type="text/plain")

    counters.set_phase("stopped" if run_status == "stopped" else "completed")
    counters.flush(force=True)
    return summary
