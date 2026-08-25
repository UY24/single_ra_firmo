# backend/app/services/serpwow/relationship_runner.py
"""Relationship run orchestration: scrape -> verdict -> outputs.

The generic half — one message per run, the bounded task window, stop gating, task
draining, the single-flight guard, the stale-run re-drive, the consumer and the
Supabase/Slack terminal write — lives in s3_run_driver and is shared with gmaps. What is
here is what this pipeline actually does: one AI Mode call per row, then the Gemini Batch
verdict phase, then the outputs.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import aio_pika

from app.services.common import llm_batch
from app.services.common.env import get_int_env as _get_int_env
from app.services.serpwow import s3_run_driver as driver
from app.services.serpwow import s3_run_store as store
from app.services.serpwow.modes.relationship import (
    ai_mode_arrays,
    build_evidence,
    row_fields,
)
from app.services.serpwow.query_builders import build_relationship_search_query
from app.services.serpwow.relationship_outputs import write_outputs
from app.services.serpwow.row_logging import _log_row_stage
from app.services.serpwow.scrapedo_ai_client import search_ai_mode
from app.services.serpwow.url_utils import x_domain_from_input_url

_LOGGER = logging.getLogger(__name__)

RELATIONSHIP_QUEUE = "relationship_runs"
RELATIONSHIP_ROUTING_KEY = "relationship.run"
async def _scrape_one(prefix: str, row: dict[str, Any], counters: store.Counters) -> None:
    fields = row_fields(row)
    idx = int(fields["row_index"])
    x_domain = x_domain_from_input_url(fields["input_url"])
    query = build_relationship_search_query(
        x_name=fields["x_name"], y_name=fields["y_name"], x_domain=x_domain,
        input_url=fields["input_url"])

    # gl stays at the client default ("us"): the CSV carries no location to derive it
    # from, and AI Mode answers a company question the same way from any locale.
    envelope = await search_ai_mode(query)
    envelope["row_index"] = idx
    envelope["fields"] = fields
    envelope["x_domain"] = x_domain

    counters.bump(requests=int(envelope.get("request_count") or 0),
                  credits=int(envelope.get("credits") or 0))

    try:
        await asyncio.to_thread(store.write_row, prefix, idx, envelope)
    except Exception as exc:
        # S3 is the ONLY copy. A failed PUT means this row's work is gone, so it must
        # count as failed and be redone on the next drive — never silently swallowed.
        _LOGGER.warning("relationship row %s: S3 put failed: %s: %s",
                        idx, type(exc).__name__, exc)
        counters.bump(rows_failed=1)
        return

    if envelope.get("error"):
        counters.bump(rows_failed=1)
    else:
        counters.bump(rows_scraped=1)
        if envelope.get("billed_empty"):
            counters.bump(rows_billed_empty=1)
    counters.flush()

    # Per-row trace via the repo's existing helper, which PRINTS. _LOGGER alone was the
    # reason `python worker.py` sat silent through a whole relationship run while gmaps and
    # AI Mode filled the terminal: nothing configures a logging handler in the worker.
    blocks, refs = ai_mode_arrays(envelope)
    _log_row_stage(
        "relationship.scrape",
        (f"y={fields['y_name']!r} refs={len(refs)} "
         f"blocks={len(blocks)} "
         f"attempts={envelope.get('request_count')} credits={envelope.get('credits')}"
         + (f" billed_empty=1" if envelope.get("billed_empty") else "")
         + (f" error={envelope['error']}" if envelope.get("error") else "")),
        upload_id=prefix.rsplit("/", 1)[-1],
        row_index=idx,
        level="WARN" if envelope.get("error") else "INFO",
    )


async def run_scrape_phase(prefix: str, counters: store.Counters) -> None:
    """Scrape every row that has no object yet, `SCRAPEDO_CONCURRENCY` at a time.

    A relationship row's raw/ object is written last, so raw/ IS the completion marker —
    the driver's default done_prefix.
    """
    await driver.run_row_phase(
        prefix, counters,
        lambda row: _scrape_one(prefix, row, counters),
        label="relationship")


def _shard_size() -> int:
    return llm_batch.shard_size()


def _max_inflight() -> int:
    """Concurrent Gemini batch jobs.

    At 500k rows and the default 5, this is 100 shards in 20 SEQUENTIAL waves, and the
    Batch API targets turnaround within 24h per job — so this knob, not the scraping,
    is the likely wall-clock bottleneck.

    Do NOT just raise this: on its own it does nothing above ~6. Every shard is submitted
    and polled through `asyncio.to_thread`, which uses the event loop's DEFAULT executor
    (`ThreadPoolExecutor(max_workers=min(32, cpu_count + 4))` — 6 on a 2-vCPU box), and
    submit() holds its thread for the shard's entire multi-hour poll. Set this above that
    and the extra shards simply queue on the executor, never created, with no error. The
    PREREQUISITE is a sized executor (`loop.set_default_executor(ThreadPoolExecutor(...))`
    at worker startup, or an explicit per-shard executor) — deliberately not done yet.
    """
    return llm_batch.max_inflight()


def _batch_timeout_sec() -> int:
    """How long to keep polling ONE Gemini shard.

    Its OWN key, not the shared `GEMINI_BATCH_TIMEOUT_SEC`: that one bounds gsearch's
    per-row batches and is set to 1800 in real deployments, which would abandon every
    relationship shard after 30 minutes when the Batch API's own target is "within 24h
    per job". Default 48h = Gemini's hard job expiry, past which the job cannot succeed.
    """
    return llm_batch.timeout_sec()


def _poll_to_terminal(gb, name: str, counters: store.Counters | None) -> dict:
    """Block until this Gemini job is terminal; return the job object.

    Bounded, unlike the `while True` this replaced. A shard that never terminalises used
    to park the thread forever: the run sat in phase="cleaning" with the heartbeat keeping
    it looking healthy, so redrive_stale_runs never rescued it either.

    ``counters``, when given, is force-flushed every poll — a heartbeat. Without it a run
    legitimately waiting hours on one batch writes status.json once and then goes silent,
    blowing past RELATIONSHIP_STALE_SEC and making redrive_stale_runs start a second,
    re-spending drive on top of a perfectly healthy run.
    """
    import time as _time

    deadline = _time.monotonic() + _batch_timeout_sec()
    while True:
        obj = gb.get_batch(name)
        if gb.is_terminal(gb.state_name(obj), bool(obj.get("done"))):
            return obj
        if _time.monotonic() >= deadline:
            # Raise rather than return {}: _drain counts it and the run reports
            # completed_with_errors. The rows keep no cleaned/ object AND the batch
            # record survives, so the next drive RE-ATTACHES to this same job instead of
            # paying for a second one (see _reattach_batches).
            raise TimeoutError(
                f"Gemini batch {name} did not finish within "
                f"GEMINI_BATCH_TIMEOUT_SEC")
        if counters is not None:
            counters.flush(force=True)
        _time.sleep(llm_batch.poll_sec())


def _abort_if_shard_died(gb, obj: dict, record_key: str, name: str) -> None:
    """Raise (and forget the job) when Google answered nothing for this shard.

    Deliberately does NOT write cleaned/: those rows keep their pending marker, so the next
    drive resubmits ONLY them. And it drops the job record first, or the next drive would
    re-attach to a job that can never answer instead of submitting a fresh one.

    The scrape is untouched by any of this — raw/ and rows/ already exist, and every phase
    skips a row that has an object, so retrying the LLM half re-buys nothing from scrape.do.
    """
    reason = llm_batch.shard_failure(gb, obj)
    if reason is None:
        return
    try:
        store.delete_object(record_key)
    except Exception:                       # best-effort: the raise below matters more
        pass
    raise RuntimeError(
        f"Gemini batch {name} ended {reason} with no results; "
        f"its rows stay pending for the next drive")


def _collect(gb, obj: dict, model: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for record in gb.collect_results(obj):
        key = str(record.get("key") or "")
        parsed = gb.parse_json_from_text(record.get("text") or "")
        if key:
            # Keep `usage` and `model`, not just the verdict. collect_results already
            # hands back per-request usageMetadata and we were throwing it away, which is
            # why the run-detail Model chip and the Input/Output-token and LLM-cost tiles
            # went blank for this pipeline — the UI reads them, nothing fed them.
            out[key] = {"parsed": parsed or {}, "usage": record.get("usage"),
                        "model": model}
    return out


def _run_gemini_batch(prefix: str, items: list[tuple[str, dict]],
                       counters: store.Counters | None = None) -> dict[str, dict]:
    """Submit one shard and block until it is terminal; returns {key: result}.

    Seam for tests — patched in test_relationship_runner. Sync on purpose: callers wrap
    it in asyncio.to_thread.

    The job NAME is written to S3 before the first poll. A Gemini batch runs on Google's
    side and is billed whether or not we are still listening, so losing the name (worker
    restart, timeout, crash) used to mean paying for the shard twice: the next drive saw
    rows with no verdict and submitted an identical batch. The record is what lets a
    re-drive re-attach instead. It is cleared by the caller, once the verdicts are
    durable — not here, or a crash in between would lose both the name and the results.
    """
    from app.services.ai_mode import gemini_batch as gb

    if not items:
        return {}
    # Was GEMINI_BATCH_MODEL with NO GEMINI_MODEL fallback, so setting only
    # GEMINI_MODEL moved the other three pipelines and left this one behind.
    model = llm_batch.batch_model()
    created = gb.create_batch(model, items, display_name=f"relationship-{prefix}")
    name = gb.batch_name_from_create(created)
    indices = sorted(int(key) for key, _body in items)
    try:
        store.put_object(store.batch_record_key(prefix, indices),
                         {"name": name, "model": model, "indices": indices})
    except Exception as exc:
        # BEST-EFFORT, unlike every other write in this pipeline. put_object raises by
        # design, but raising here would abandon a batch Google is already billing us for
        # — the exact double-spend this record exists to prevent. This process still has
        # the name in memory and polls on regardless; only crash-recovery is lost.
        _LOGGER.warning("relationship %s: could not record batch %s (%s: %s) — "
                        "a restart before it resolves will resubmit these rows",
                        prefix, name, type(exc).__name__, exc)
    obj = _poll_to_terminal(gb, name, counters)
    _abort_if_shard_died(gb, obj, store.batch_record_key(prefix, indices), name)
    return _collect(gb, obj, model)


async def _iter_scraped_rows(prefix: str, wanted: set[int]):
    """Yield (idx, envelope) for each wanted row that has a provider response.

    The envelope's CSV fields come from streaming input.csv, not from a per-row sidecar
    object in S3: they were already in the CSV, and a sidecar would have meant a second
    GET per row on top of the response — 500k extra round-trips in this phase alone.
    """
    it = store.iter_input_rows(prefix)
    while True:
        row = await asyncio.to_thread(driver._next_or_exhausted, it)
        if row is driver._EXHAUSTED:
            return
        idx = int(row["row_index"])
        if idx not in wanted:
            continue
        envelope = await asyncio.to_thread(store.read_row, prefix, idx)
        if not envelope or envelope.get("response") is None:
            continue   # an error row: no body, so no verdict to seek
        fields = row_fields(row)
        envelope["row_index"] = idx
        envelope["fields"] = fields
        envelope["x_domain"] = x_domain_from_input_url(fields["input_url"])
        yield idx, envelope


async def _shard_meta(prefix: str, wanted: set[int]) -> dict[str, tuple[list[str], str]]:
    """{row key: (candidates, x_domain)} for rows whose verdicts came from a batch this
    process did not submit. Rebuilt locally from the stored response — no LLM, no scrape."""
    out: dict[str, tuple[list[str], str]] = {}
    async for idx, envelope in _iter_scraped_rows(prefix, wanted):
        _key, _body, candidates, x_domain = _build_batch_item(envelope)
        out[str(idx)] = (candidates, x_domain)
    return out


async def _reattach_batches(prefix: str, counters: store.Counters) -> None:
    """Finish any shard a previous drive paid for but never collected.

    Runs before this drive computes its own pending set, so those rows get their verdicts
    from the batch Google already ran instead of being resubmitted. Rows that DID land a
    cleaned object are skipped, which makes a stale record (deleted mid-flight, or written
    by a drive that then completed normally) free to clear.
    """
    from app.services.ai_mode import gemini_batch as gb

    records = await asyncio.to_thread(store.list_batch_records, prefix)
    if not records:
        return
    cleaned_already = await asyncio.to_thread(store.list_cleaned_rows, prefix)
    outstanding = {
        record["record_key"]: [int(i) for i in (record.get("indices") or [])
                               if int(i) not in cleaned_already]
        for record in records
    }
    for record in records:
        if not outstanding[record["record_key"]]:
            await asyncio.to_thread(store.delete_object, record["record_key"])
    live = [r for r in records if outstanding[r["record_key"]]]
    if not live:
        return
    # ONE csv pass for every outstanding record, not one per record.
    meta = await _shard_meta(prefix, {i for r in live for i in outstanding[r["record_key"]]})
    for record in live:
        pending = outstanding[record["record_key"]]
        name = str(record["name"])
        model = str(record.get("model") or "")
        _log_row_stage("relationship.phase",
                       f"LLM phase: re-attaching to Gemini batch {name} "
                       f"({len(pending)} row(s) still without a verdict)",
                       upload_id=prefix.rsplit("/", 1)[-1])
        obj = await asyncio.to_thread(_poll_to_terminal, gb, name, counters)
        await asyncio.to_thread(
            _abort_if_shard_died, gb, obj, record["record_key"], name)
        results = await asyncio.to_thread(_collect, gb, obj, model)
        await _write_verdicts(prefix, [str(i) for i in pending], results, counters,
                              meta=meta)
        await asyncio.to_thread(store.delete_object, record["record_key"])


async def _write_verdicts(prefix: str, keys: list[str], results: dict[str, dict],
                          counters: store.Counters,
                          meta: dict[str, tuple[list[str], str]] | None = None) -> None:
    """Persist one shard's verdicts as cleaned/ objects, one per row.

    ``meta`` carries the candidate set this drive already built. It is absent only when
    re-attaching to a shard a PREVIOUS drive submitted, in which case it is rebuilt from
    the row's raw object — a local re-parse, no LLM call and no scrape. Passing it on the
    normal path keeps this from adding a second S3 GET per row.
    """
    for key in keys:
        idx = int(key)
        if meta is not None:
            candidates, x_domain = meta.get(key, ([], ""))
        else:
            envelope = await asyncio.to_thread(store.read_row, prefix, idx)
            candidates, x_domain = ([], "")
            if envelope:
                _k, _b, candidates, x_domain = _build_batch_item(envelope)
        result = results.get(key) or {}
        await asyncio.to_thread(
            store.put_object, store.cleaned_key(prefix, idx),
            {"row_index": idx, "parsed": result.get("parsed"),
             # Stored so phase 3 reads cleaned/ alone: re-deriving the candidate set
             # would mean fetching every raw object a second time.
             "candidates": candidates, "x_domain": x_domain, "error": None,
             # Per-row LLM usage + model, so write_outputs can report token counts,
             # the model name and the Gemini cost the UI already has tiles for.
             "usage": result.get("usage"), "model": result.get("model")})
        counters.bump(rows_cleaned=1)
    counters.flush()


def _build_batch_item(envelope: dict[str, Any]) -> tuple[str, dict, list[str], str]:
    """(key, request_body, candidates, x_domain) for one scraped row."""
    from app.services.ai_mode import gemini_batch as gb
    from app.services.serpwow.gemini_llm import build_relationship_prompt

    fields = envelope.get("fields") or {}
    x_domain = str(envelope.get("x_domain") or "")
    evidence = build_evidence(envelope, x_domain)
    prompt = build_relationship_prompt(
        x_name=fields.get("x_name") or "",
        y_name=fields.get("y_name") or "",
        input_url=fields.get("input_url") or "",
        candidates=evidence["candidates"],
        ai_overview_evidence=evidence["ai_overview_evidence"],
        search_attempts=evidence["search_attempts"],
        x_domain=x_domain,
    )
    key = str(int(envelope.get("row_index")))
    body = gb.messages_to_gemini_request([{"role": "user", "content": prompt}])
    return key, body, evidence["candidates"], x_domain


async def run_verdict_phase(prefix: str, counters: store.Counters) -> None:
    """Turn every scraped row into a verdict via the Gemini Batch API.

    Raw objects are GET'd just-in-time to build each request body, so no run-wide list
    of payloads exists. Rows that already have a cleaned object are skipped, which makes
    a re-drive redo only the LLM work that is actually missing.
    """
    counters.set_phase("cleaning")
    counters.flush(force=True)
    _log_row_stage("relationship.phase",
                   "LLM phase: submitting Gemini Batch verdict shards "
                   f"(shard_size={_shard_size()} max_inflight={_max_inflight()})",
                   upload_id=prefix.rsplit("/", 1)[-1])

    # BEFORE computing pending: collect any shard a previous drive paid Google for but
    # never got the answer to. Skipping this would resubmit those rows and pay twice.
    await _reattach_batches(prefix, counters)

    scraped = await asyncio.to_thread(store.list_done_rows, prefix)
    already = await asyncio.to_thread(store.list_cleaned_rows, prefix)
    pending = scraped - already

    shard: list[tuple[str, dict]] = []
    meta: dict[str, tuple[list[str], str]] = {}
    inflight: set[asyncio.Task] = set()

    async def submit(items: list[tuple[str, dict]],
                     item_meta: dict[str, tuple[list[str], str]]) -> None:
        results = await asyncio.to_thread(_run_gemini_batch, prefix, items, counters)
        keys = [key for key, _body in items]
        await _write_verdicts(prefix, keys, results, counters, meta=item_meta)
        # Only now that every verdict is durable is the in-flight record safe to clear.
        if keys:
            await asyncio.to_thread(
                store.delete_object,
                store.batch_record_key(prefix, sorted(int(k) for k in keys)))

    stopped = False
    async for idx, envelope in _iter_scraped_rows(prefix, pending):
        key, body, candidates, x_domain = _build_batch_item(envelope)
        if not candidates and not ai_mode_arrays(envelope)[0]:
            # ZERO EVIDENCE (the billed_empty case: HTTP 200, no text_blocks, no
            # references). The envelope parses fine, so the old skip-if-unparseable gate
            # let it into a shard and paid for it — then build_row_result's has_evidence
            # check (modes/relationship.py, ordered ahead of `parsed is None`) threw the
            # answer away and stamped REL_ERROR_NO_EVIDENCE anyway. ~15k pointless Batch
            # requests at 500k rows with 3% empty responses. Same gate, hoisted to before
            # the money: write the cleaned object directly with parsed=None so phase 3
            # reaches the identical verdict and a re-drive skips the row too.
            await asyncio.to_thread(
                store.put_object, store.cleaned_key(prefix, idx),
                {"row_index": idx, "parsed": None, "candidates": candidates,
                 "x_domain": x_domain, "error": None})
            counters.bump(rows_cleaned=1)
            counters.flush()
            continue
        shard.append((key, body))
        meta[key] = (candidates, x_domain)
        if len(shard) >= _shard_size():
            # Stop check at the SHARD boundary — once per GEMINI_BATCH_SHARD_SIZE rows,
            # not per row: this is the only thing between a stop at row 1 of a 500k run
            # and buying all 100 remaining Gemini shards. Checked BEFORE submitting the
            # full shard, so a stop costs zero further batches (shards already in flight
            # are paid for and run to completion — that latency is accepted).
            if await asyncio.to_thread(store.stop_requested, prefix):
                _LOGGER.info("relationship %s: stop requested, submitting no more shards",
                             prefix)
                stopped = True
                break
            while len(inflight) >= _max_inflight():
                finished, inflight = await asyncio.wait(
                    inflight, return_when=asyncio.FIRST_COMPLETED)
                driver.drain(finished, counters, "verdict")
            inflight.add(asyncio.create_task(submit(shard, meta)))
            shard, meta = [], {}

    # Always flush the tail shard, even if empty: _run_gemini_batch's own "if not
    # items: return {}" guard makes an empty flush a no-op (no batch job created, no
    # spend) — but it's what lets a run with zero net-pending rows (e.g. every
    # remaining row was an error marker) still resolve deterministically through the
    # same seam the tests patch, instead of a special empty-run path. Skipped after a
    # stop: the partial shard accumulated since the last boundary must not be bought.
    if not stopped:
        inflight.add(asyncio.create_task(submit(shard, meta)))
    if inflight:
        finished, _ = await asyncio.wait(inflight)
        driver.drain(finished, counters, "verdict")
    counters.flush(force=True)


async def _phases(prefix: str, counters: store.Counters,
                  pointer: dict[str, Any]) -> dict[str, Any]:
    """This pipeline's actual work: scrape, then the Gemini Batch verdict pass, then the
    outputs. Everything around it is s3_run_driver.drive."""
    _phase = time.monotonic()
    await run_scrape_phase(prefix, counters)
    counters.bump(scrape_seconds=int(time.monotonic() - _phase))
    # A stop does NOT discard the run. Skip only the (money-spending) verdict phase:
    # write_outputs already handles rows with no cleaned/ object fine (parsed=None ->
    # "unclear"), and it is what produces confirmed_relation.csv, labels the run "stopped",
    # and gives the terminal notify something to send. Returning here instead threw away
    # every scraped row of a multi-day run, left Supabase on "queued" forever, and showed a
    # completed run with an empty Files card.
    if not await asyncio.to_thread(store.stop_requested, prefix):
        _phase = time.monotonic()
        await run_verdict_phase(prefix, counters)
        counters.bump(llm_seconds=int(time.monotonic() - _phase))
    return await asyncio.to_thread(write_outputs, prefix, counters)


async def drive_run(run_id: str) -> None:
    """Run (or resume) one relationship run end to end. Idempotent and never raises."""
    await driver.drive(
        run_id,
        segment=store.PIPELINE_SEGMENT,
        phases=_phases,
        pipeline_label="relationship",
        search_label="Scrape.do searches",
    )


async def redrive_stale_runs() -> int:
    """Re-drive relationship runs that stopped making progress (see s3_run_driver)."""
    return await driver.redrive_stale(
        segment=store.PIPELINE_SEGMENT,
        drive_fn=drive_run,
        stale_sec=driver.stale_seconds("RELATIONSHIP_STALE_SEC"),
        max_attempts=driver.max_drive_attempts("RELATIONSHIP_MAX_DRIVE_ATTEMPTS"),
    )


async def consume_relationship_runs(channel) -> None:
    """Declare the relationship run queue and start consuming (see s3_run_driver)."""
    await driver.consume_runs(
        channel,
        queue_name=RELATIONSHIP_QUEUE,
        routing_key=RELATIONSHIP_ROUTING_KEY,
        drive_fn=drive_run,
        label="relationship",
    )
