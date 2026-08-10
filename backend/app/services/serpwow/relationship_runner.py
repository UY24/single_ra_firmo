# backend/app/services/serpwow/relationship_runner.py
"""Relationship run orchestration: scrape -> verdict -> outputs.

ONE RabbitMQ message carries a whole run. The message body is just {"run_id": ...}; the
rows live in input.csv on S3, and parallelism comes from the bounded task window in here
(topped up to the scrape.do account cap `SCRAPEDO_CONCURRENCY` and refilled as tasks
finish), not from the message count. A single async process in this repo has already held
60 concurrent scrape.do calls, so the broker is a durable start signal, not a work
distributor. Note `WORKER_CONCURRENCY` is irrelevant here: it sets RabbitMQ prefetch, and
this pipeline has exactly one message per run.

ponytail: the _driving single-flight guard below is IN-PROCESS ONLY (a module-level
set). It is correct today because the repo runs exactly one worker process. A second
worker process would let two drivers race the same run and double-submit an in-flight
Gemini shard — real re-spend. Upgrade path: a distributed lease, e.g. an S3
conditional-put (IfNoneMatch) lock object under the run prefix, renewed by the driver
and expired on crash.
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

from app.services.common.env import get_int_env as _get_int_env
from app.services.common.provider_limits import scrapedo_limit
from app.services.serpwow import relationship_store as store
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
# Genuinely finished — never re-driven. "failed" is deliberately NOT here: it means
# drive_run hit an exception, which over a multi-day run is usually something transient
# (an S3 stream drop, a SlowDown past boto3's retries), and a re-drive is free because
# every row that already has an object is skipped. See _redrivable_failure.
_TERMINAL_PHASES = {"completed", "stopped"}

# In-process single-flight guard: run_ids currently inside drive_run. redrive_stale_runs
# and the queue consumer are two independent paths that can both decide to drive the same
# run_id — the object-presence skip in each phase only protects against re-spend on rows a
# PRIOR (finished) drive already did; it does nothing to stop two drivers racing on the
# same in-flight Gemini shard *right now*. Checked and cleared in drive_run itself.
_driving: set[str] = set()

# Bounds stop-marker latency deterministically. With independently varying scrape
# latencies, asyncio.wait(FIRST_COMPLETED) returns roughly one task at a time, so
# checking "once per outer-loop iteration" is effectively once per row in steady state
# (measured: 218 checks over 300 rows at limit=100, i.e. 73%) — a wall-clock gate is the
# only bound that holds regardless of how completions happen to interleave.
_STOP_CHECK_INTERVAL_SEC = 1.0


def _drain(done: set[asyncio.Task], counters: store.Counters, phase: str) -> None:
    """Retrieve every finished task's exception, log it, and count it.

    NOT optional bookkeeping. asyncio.wait/gather hand back Task objects whose exception
    is only realised when someone asks for it: an unretrieved one surfaces as a GC-time
    "Task exception was never retrieved" warning and nothing else. Both phases have a
    failure mode that is otherwise completely silent — _scrape_one formats the
    OPERATOR-EDITABLE prompt file, so one stray "{" makes every row raise KeyError, and
    submit() wraps create_batch/collect_results/put_object, so one Gemini quota error
    discards an already-PAID-FOR shard. The counter is what makes either visible: it
    lands in status.json and turns write_outputs' verdict into completed_with_errors
    instead of a clean "completed".
    """
    failed = 0
    for task in done:
        if task.cancelled():
            continue
        exc = task.exception()
        if exc is not None:
            failed += 1
            _LOGGER.error("relationship %s task failed: %s: %s",
                          phase, type(exc).__name__, exc, exc_info=exc)
    if failed:
        counters.bump(task_errors=failed)
        counters.flush()


def _concurrency() -> int:
    """Size of the live-task window = in-flight AI Mode calls for this pipeline.

    DERIVED from the vendor cap (`SCRAPEDO_CONCURRENCY`, default 100) rather than owning
    its own knob. A separate setting could only ever disagree with the real limit: every
    call already passes through `scrapedo_slot()`, so a window wider than the semaphore
    just parks tasks on it, and a narrower one silently throttles below what was
    configured. The window still has to exist — without it a 500k-row run would create
    500k tasks, all blocked on the same semaphore.

    Note this derives a *worker* bound FROM the *vendor* cap, which is the safe direction.
    The reverse (a vendor cap tracking `WORKER_CONCURRENCY`) is explicitly rejected in
    provider_limits: it would let raising worker slots silently raise the vendor limit.

    NOT proven at 100: on scrape.do's Maps endpoint this repo measured 100 running 1.9x
    SLOWER than 25, because scrape.do queues rather than 429ing. Re-measure on a 100-row
    run before trusting it.
    """
    return max(1, scrapedo_limit())


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
    """Scrape every row that has no object yet, `SCRAPEDO_CONCURRENCY` at a time."""
    counters.set_phase("scraping")
    counters.flush(force=True)

    done = await asyncio.to_thread(store.list_done_rows, prefix)
    _log_row_stage("relationship.phase",
                   f"scrape phase: {len(done)} row(s) already done, concurrency="
                   f"{_concurrency()}", upload_id=prefix.rsplit("/", 1)[-1])
    _LOGGER.info("relationship %s: resuming with %d row(s) already done", prefix, len(done))

    stopped = False

    async def guarded(row: dict[str, Any]) -> None:
        if stopped:
            return
        await _scrape_one(prefix, row, counters)

    it = _iter_pending(prefix, done)
    limit = _concurrency()
    tasks: set[asyncio.Task] = set()
    exhausted = False
    # -inf forces the very first pass to check, so a stop marker set before the phase
    # starts still halts it immediately (see test_stop_marker_halts_the_phase_early).
    last_stop_check = float("-inf")

    while not exhausted or tasks:
        # Time-gated, not once-per-row and not once-per-refill: at 500k rows a per-row
        # (or per-completion) S3 GET here would serialize hundreds of thousands of
        # round-trips into the dispatch loop that gates how fast new work starts. A
        # wall-clock bound holds regardless of how task completions interleave.
        now = time.monotonic()
        if not exhausted and now - last_stop_check >= _STOP_CHECK_INTERVAL_SEC:
            last_stop_check = now
            if await asyncio.to_thread(store.stop_requested, prefix):
                stopped = True
                exhausted = True

        while not exhausted and len(tasks) < limit:
            # next() on a generator runs the generator BODY, including the blocking S3
            # CSV read — wrapping the to_thread call around the *call* that builds the
            # generator (as an earlier version of this did) only builds the generator
            # object and does no I/O, so the read would happen back on the event loop
            # the first time this loop iterates it. Wrapping next() itself keeps every
            # blocking read off the event loop while still pulling one row at a time.
            row = await asyncio.to_thread(_next_or_exhausted, it)
            if row is _EXHAUSTED:
                exhausted = True
                break
            tasks.add(asyncio.create_task(guarded(row)))

        if tasks:
            done_tasks, tasks = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED)
            _drain(done_tasks, counters, "scrape")

    counters.flush(force=True)


_EXHAUSTED = object()


def _iter_pending(prefix: str, done: set[int]):
    """Rows with no object yet. A generator, so the CSV is never fully materialised."""
    for row in store.iter_input_rows(prefix):
        if int(row["row_index"]) not in done:
            yield row


def _next_or_exhausted(it):
    """One pull off a (possibly blocking) iterator, run inside asyncio.to_thread by the
    caller so the blocking read stays off the event loop."""
    return next(it, _EXHAUSTED)


def _shard_size() -> int:
    return max(1, _get_int_env("GEMINI_BATCH_SHARD_SIZE", 5000))


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
    return max(1, _get_int_env("GEMINI_BATCH_MAX_INFLIGHT", 5))


def _batch_timeout_sec() -> int:
    """How long to keep polling ONE Gemini shard.

    Its OWN key, not the shared `GEMINI_BATCH_TIMEOUT_SEC`: that one bounds gsearch's
    per-row batches and is set to 1800 in real deployments, which would abandon every
    relationship shard after 30 minutes when the Batch API's own target is "within 24h
    per job". Default 48h = Gemini's hard job expiry, past which the job cannot succeed.
    """
    return max(60, _get_int_env("RELATIONSHIP_BATCH_TIMEOUT_SEC", 172800))


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
                f"RELATIONSHIP_BATCH_TIMEOUT_SEC")
        if counters is not None:
            counters.flush(force=True)
        _time.sleep(_get_int_env("GEMINI_BATCH_POLL_SEC", 30))


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
    model = os.getenv("GEMINI_BATCH_MODEL", "gemini-2.5-flash-lite")
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
    return _collect(gb, _poll_to_terminal(gb, name, counters), model)


async def _iter_scraped_rows(prefix: str, wanted: set[int]):
    """Yield (idx, envelope) for each wanted row that has a provider response.

    The envelope's CSV fields come from streaming input.csv, not from a per-row sidecar
    object in S3: they were already in the CSV, and a sidecar would have meant a second
    GET per row on top of the response — 500k extra round-trips in this phase alone.
    """
    it = store.iter_input_rows(prefix)
    while True:
        row = await asyncio.to_thread(_next_or_exhausted, it)
        if row is _EXHAUSTED:
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
                _drain(finished, counters, "verdict")
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
        _drain(finished, counters, "verdict")
    counters.flush(force=True)


async def drive_run(run_id: str) -> None:
    """Run (or resume) one relationship run end to end. Idempotent and never raises.

    Every phase skips work that already has an object, so calling this on a run that is
    partly or fully done costs no scrape.do credits and no Gemini tokens. That property
    only holds across SEPARATE drives, though — two drivers racing on the SAME run at the
    same time can both see the same "pending" set and both submit it, which does re-spend.
    _driving is the single-flight guard against that (see its module-level comment): the
    queue consumer and redrive_stale_runs are the two paths that can otherwise collide.
    """
    if run_id in _driving:
        _LOGGER.info(
            "relationship run %s: already being driven in this process, skipping", run_id)
        return
    _driving.add(run_id)
    try:
        pointer = await asyncio.to_thread(store.read_run_pointer, run_id)
        if not pointer:
            _LOGGER.warning(
                "relationship run %s: no pointer object, nothing to drive", run_id)
            return
        prefix = str(pointer.get("prefix") or "")
        status = await asyncio.to_thread(store.read_status, prefix) or {}
        # created_at is carried forward, never re-stamped: it is what makes total wall
        # clock survive a worker restart mid-run.
        counters = store.Counters(prefix, rows_total=int(status.get("rows_total") or 0),
                                  created_at=status.get("created_at"))
        for field in counters.values:
            counters.values[field] = int(status.get(field) or 0)

        # create_run stamps "queued" and _notify_terminal was the ONLY other update_run
        # call, so a healthy 6-day 500k run read "queued" in the Runs list for its whole
        # life. Best-effort, like every Supabase write in this pipeline.
        await asyncio.to_thread(_update_supabase, pointer, status="running")

        try:
            _phase = time.monotonic()
            await run_scrape_phase(prefix, counters)
            counters.bump(scrape_seconds=int(time.monotonic() - _phase))
            # A stop does NOT discard the run. Skip only the (money-spending) verdict
            # phase: write_outputs already handles rows with no cleaned/ object fine
            # (parsed=None -> "unclear"), and it is what produces confirmed_relation.csv,
            # labels the run "stopped", and gives _notify_terminal something to send.
            # Returning here instead threw away every scraped row of a multi-day run,
            # left Supabase on "queued" forever, and showed a completed run with an
            # empty Files card.
            if not await asyncio.to_thread(store.stop_requested, prefix):
                _phase = time.monotonic()
                await run_verdict_phase(prefix, counters)
                counters.bump(llm_seconds=int(time.monotonic() - _phase))
            summary = await asyncio.to_thread(write_outputs, prefix, counters)
            await asyncio.to_thread(_notify_terminal, run_id, pointer, summary)
        except Exception as exc:
            _LOGGER.exception("relationship run %s failed: %s", run_id, exc)
            # Counted so the re-drive scan can retry a bounded number of times — one
            # transient error must not park a multi-day run permanently.
            counters.bump(drive_attempts=1)
            counters.set_phase("failed")
            counters.flush(force=True)
            # Terminalize Supabase too, or the Runs list shows "queued" forever for a run
            # that is never coming back.
            await asyncio.to_thread(
                _update_supabase, pointer, status="failed",
                finished_at=datetime.now(timezone.utc).isoformat())
    finally:
        _driving.discard(run_id)


def _update_supabase(pointer: dict[str, Any], **fields: Any) -> None:
    """Best-effort partial update of this run's Supabase row. NEVER raises.

    Deliberately NOT routed through engine._update_supabase_run: that helper is
    rows-shaped for gsearch/gmaps (it rebuilds success/failed counts, websites_found/
    not_found, cost, AND file_links from state["rows"]/state["upload_id"]), and this
    pipeline keeps none of that — every one of those would come back zeroed or empty
    instead of using the numbers write_outputs already computed. update_run() itself is
    just a partial `.update(fields)`, so calling it directly with the real numbers is no
    smaller a diff than fighting the shared helper's rows assumption, and it's correct.

    A relationship run has no state dict, so run_db_id rides in the pointer; no id means
    Supabase was never configured for this run and there is nothing to update.
    """
    try:
        from app.services.companies import get_company_service

        run_db_id = pointer.get("run_db_id")
        if not run_db_id:
            return
        svc = get_company_service()
        if svc is not None:
            svc.update_run(run_db_id, **fields)
    except Exception:
        pass


def _notify_terminal(run_id: str, pointer: dict[str, Any], summary: dict[str, Any]) -> None:
    """Best-effort Slack + Supabase at terminal status. Never raises — bookkeeping must
    not fail a run that already produced its outputs.

    "stopped" (see relationship_outputs.write_outputs) is a status the Runs list UI has no
    specific CSS rule for — confirmed safe: static/js/ui.js's statusBadge() renders any
    string as plain text with a data-status attribute, and app.css's unmatched-selector
    fallback is just the neutral base badge style, not blank/broken.
    """
    cost = summary.get("cost") or {}
    outcomes = summary.get("outcome_breakdown") or {}
    # write_outputs threads a distinct "stopped" status through when a stop was
    # requested (the scrape-phase stop skips only the verdict phase now, so a stopped run
    # reaches here too); fall back to the 2-way derivation for older summaries that
    # predate that field.
    status = summary.get("status") or (
        "completed_with_errors" if outcomes.get("errored") else "completed")

    _update_supabase(
        pointer,
        status=status,
        total_rows=summary.get("total_rows"),
        success_count=summary.get("websites_found"),
        failed_count=outcomes.get("errored"),
        websites_found=summary.get("websites_found"),
        websites_not_found=summary.get("websites_not_found"),
        cost=cost,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )

    try:
        from app.core.notify import notify_run_complete

        notify_run_complete(
            pipeline="relationship",
            company=pointer.get("company_name"),
            run_ref=run_id,
            status=status,
            found=summary.get("websites_found"),
            not_found=summary.get("websites_not_found"),
            errored=outcomes.get("errored"),
            total_rows=summary.get("total_rows"),
            searches=cost.get("scrapedo_requests"),
            search_label="Scrape.do searches",
            credits=cost.get("scrapedo_credits"),
        )
    except Exception:
        pass


def _stale_seconds() -> int:
    return max(60, _get_int_env("RELATIONSHIP_STALE_SEC", 900))


def _age_seconds(updated_at: str) -> float:
    try:
        parsed = time.strptime(str(updated_at), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return float("inf")
    return max(0.0, time.time() - calendar.timegm(parsed))


def _max_drive_attempts() -> int:
    return max(1, _get_int_env("RELATIONSHIP_MAX_DRIVE_ATTEMPTS", 3))


def _redrivable(status: dict[str, Any]) -> bool:
    """Should the scan pick this run up?

    A run whose phase is "failed" is worth retrying: drive_run died on an exception, and
    on a multi-day run that is usually transient. A re-drive costs nothing — every row
    with an object is skipped, so no scrape.do credits and no Gemini tokens — which is why
    "failed" used to be a dead end for no good reason: recovery meant hand-typing the run
    id into the Operations page. Bounded by drive_attempts so a genuinely broken run still
    stops instead of looping forever.
    """
    phase = str(status.get("phase") or "")
    if phase in _TERMINAL_PHASES:
        return False
    if phase == "failed":
        return int(status.get("drive_attempts") or 0) < _max_drive_attempts()
    return True


async def redrive_stale_runs() -> int:
    """Re-drive runs that stopped making progress. Returns how many were re-driven.

    This is the ONLY self-healing machinery this pipeline needs, and it replaces
    ack-after-persist: a per-run message held for hours would hit RabbitMQ's 30-minute
    consumer_timeout and have its channel torn down, so the consumer acks on receipt
    instead. This scan also covers what a redelivery never could — the worker being down
    when the run was published, or the instance being replaced.

    Safe to run at any time: a re-drive skips every row that already has an object.
    """
    driven = 0
    for pointer in await asyncio.to_thread(store.list_run_pointers):
        prefix = str(pointer.get("prefix") or "")
        run_id = str(pointer.get("run_id") or "")
        if not prefix or not run_id:
            continue
        status = await asyncio.to_thread(store.read_status, prefix) or {}
        if not _redrivable(status):
            continue
        # The staleness gate applies to failed runs too, so a run that dies instantly
        # cannot hot-loop through its attempts in one scan pass.
        if _age_seconds(status.get("updated_at")) < _stale_seconds():
            continue
        _LOGGER.info("relationship %s: stale, re-driving", run_id)
        await drive_run(run_id)
        driven += 1
    return driven


async def consume_relationship_runs(channel) -> None:
    """Declare the run queue, bind it to the shared exchange, and start consuming.

    Own queue and channel, NOT the shared SerpWow queue: a run message occupies its
    consumer for the whole run, so on the shared queue it would permanently eat one of
    WORKER_CONCURRENCY's slots. Bound to the SAME direct exchange AI Mode uses
    (RABBITMQ_EXCHANGE, default "singleRA_search" — see ai_mode/broker.py) under
    RELATIONSHIP_ROUTING_KEY, which is where engine.publish_relationship_run publishes.

    (Publishing to the default exchange by queue name would also reach this queue —
    every AMQP queue keeps its implicit default-exchange binding — but the named
    exchange is what AI Mode and SerpWow use, and it makes the binding visible in the
    management UI instead of implicit.)
    """
    exchange = await channel.declare_exchange(
        os.getenv("RABBITMQ_EXCHANGE", "singleRA_search"),
        aio_pika.ExchangeType.DIRECT, durable=True)
    queue = await channel.declare_queue(RELATIONSHIP_QUEUE, durable=True)
    await queue.bind(exchange, routing_key=RELATIONSHIP_ROUTING_KEY)

    async def on_message(message) -> None:
        # Ack FIRST — see redrive_stale_runs for why this pipeline inverts the repo's
        # usual ack-after-persist rule.
        await message.ack()
        try:
            body = json.loads(message.body.decode("utf-8"))
        except Exception:
            _LOGGER.warning("relationship: undecodable run message, dropped")
            return
        # body can be valid JSON that isn't an object (e.g. a bare list) — .get() on
        # that would raise AttributeError, so guard rather than let a malformed-but-
        # parseable message crash the consumer.
        run_id = str(body.get("run_id") or "") if isinstance(body, dict) else ""
        if run_id:
            await drive_run(run_id)

    await queue.consume(on_message)
