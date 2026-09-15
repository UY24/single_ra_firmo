# backend/app/services/serpwow/s3_run_driver.py
"""The run-driving machinery shared by the S3-only pipelines (relationship, gmaps).

ONE RabbitMQ message carries a whole run. The body is just {"run_id": ...}; the rows live
in input.csv on S3, and parallelism comes from the bounded task window in here (sized to
the scrape.do account cap `SCRAPEDO_CONCURRENCY` and refilled as tasks finish), not from
the message count. A single async process in this repo has already held 60 concurrent
scrape.do calls, so the broker is a durable start signal, not a work distributor.
`WORKER_CONCURRENCY` is irrelevant here: it sets RabbitMQ prefetch, and these pipelines
have exactly one message per run.

Extracted from relationship_runner when gmaps became the second pipeline to need it. What
lives here is what is genuinely identical between them — the task window, stop gating,
task draining, the single-flight guard, the stale-run re-drive, the consumer, and the
Supabase/Slack terminal write. What each pipeline keeps for itself is what it actually
does: its per-row work, its phase sequence, and its outputs.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterator, Optional

import aio_pika

from app.services.common.env import get_int_env as _get_int_env
from app.services.common.provider_limits import scrapedo_limit
from app.services.serpwow import s3_run_store as store

_LOGGER = logging.getLogger(__name__)

# Genuinely finished — never re-driven. "failed" is deliberately NOT here: it means the
# driver hit an exception, which over a multi-day run is usually something transient (an
# S3 stream drop, a SlowDown past boto3's retries), and a re-drive is free because every
# row that already has an object is skipped. See _redrivable.
TERMINAL_PHASES = {"completed", "stopped"}

# In-process single-flight guard: run_ids currently being driven. The queue consumer and
# the stale-run scan are two independent paths that can both decide to drive the same
# run_id — the object-presence skip in each phase only protects against re-spend on rows a
# PRIOR (finished) drive already did; it does nothing to stop two drivers racing right
# now. Keyed by run_id, shared across pipelines (run ids are unique).
#
# ponytail: IN-PROCESS ONLY (a module-level set). Correct today because the repo runs
# exactly one worker process. A second worker would let two drivers race the same run.
# Upgrade path: a distributed lease, e.g. an S3 conditional-put (IfNoneMatch) lock object
# under the run prefix, renewed by the driver and expired on crash.
_driving: set[str] = set()

# Bounds stop-marker latency deterministically. With independently varying scrape
# latencies, asyncio.wait(FIRST_COMPLETED) returns roughly one task at a time, so checking
# "once per outer-loop iteration" is effectively once per row in steady state (measured:
# 218 checks over 300 rows at limit=100, i.e. 73%) — a wall-clock gate is the only bound
# that holds regardless of how completions happen to interleave.
_STOP_CHECK_INTERVAL_SEC = 1.0

_EXHAUSTED = object()


def concurrency() -> int:
    """Size of the live-task window = in-flight scrape.do calls for one run.

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
    SLOWER than 25, because scrape.do queues rather than 429ing. Re-measure before
    trusting it.
    """
    return max(1, scrapedo_limit())


def drain(done: set[asyncio.Task], counters: store.Counters, phase: str) -> None:
    """Retrieve every finished task's exception, log it, and count it.

    NOT optional bookkeeping. asyncio.wait/gather hand back Task objects whose exception
    is only realised when someone asks for it: an unretrieved one surfaces as a GC-time
    "Task exception was never retrieved" warning and nothing else. Every phase has a
    failure mode that is otherwise completely silent — relationship's scrape formats the
    OPERATOR-EDITABLE prompt file, so one stray "{" makes every row raise KeyError, and its
    verdict submit wraps create_batch/collect_results, so one Gemini quota error discards an
    already-PAID-FOR shard. The counter is what makes either visible: it lands in
    status.json and turns the run's verdict into completed_with_errors instead of a clean
    "completed".
    """
    failed = 0
    for task in done:
        if task.cancelled():
            continue
        exc = task.exception()
        if exc is not None:
            failed += 1
            _LOGGER.error("%s task failed: %s: %s",
                          phase, type(exc).__name__, exc, exc_info=exc)
    if failed:
        counters.bump(task_errors=failed)
        counters.flush()


def _iter_pending(prefix: str, done: set[int]) -> Iterator[dict[str, Any]]:
    """Rows with no object yet. A generator, so the CSV is never fully materialised."""
    for row in store.iter_input_rows(prefix):
        if int(row["row_index"]) not in done:
            yield row


def _next_or_exhausted(it):
    """One pull off a (possibly blocking) iterator, run inside asyncio.to_thread by the
    caller so the blocking read stays off the event loop."""
    return next(it, _EXHAUSTED)


async def run_row_phase(
    prefix: str,
    counters: store.Counters,
    do_row: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    label: str,
    done_prefix: str = "raw",
) -> None:
    """Run `do_row` over every row that has no object yet, `concurrency()` at a time.

    `done_prefix` is what "already finished" means for this pipeline — see
    s3_run_store.list_done_rows.
    """
    counters.set_phase("scraping")
    counters.flush(force=True)

    done = await asyncio.to_thread(store.list_done_rows, prefix, done_prefix)
    from app.services.serpwow.row_logging import _log_row_stage

    _log_row_stage(f"{label}.phase",
                   f"scrape phase: {len(done)} row(s) already done, concurrency="
                   f"{concurrency()}", upload_id=prefix.rsplit("/", 1)[-1])
    _LOGGER.info("%s %s: resuming with %d row(s) already done", label, prefix, len(done))

    stopped = False

    async def guarded(row: dict[str, Any]) -> None:
        if stopped:
            return
        await do_row(row)

    it = _iter_pending(prefix, done)
    limit = concurrency()
    tasks: set[asyncio.Task] = set()
    exhausted = False
    # -inf forces the very first pass to check, so a stop marker set before the phase
    # starts still halts it immediately.
    last_stop_check = float("-inf")

    while not exhausted or tasks:
        # Time-gated, not once-per-row and not once-per-refill: at 500k rows a per-row (or
        # per-completion) S3 GET here would serialize hundreds of thousands of round-trips
        # into the dispatch loop that gates how fast new work starts. A wall-clock bound
        # holds regardless of how task completions interleave.
        now = time.monotonic()
        if not exhausted and now - last_stop_check >= _STOP_CHECK_INTERVAL_SEC:
            last_stop_check = now
            if await asyncio.to_thread(store.stop_requested, prefix):
                stopped = True
                exhausted = True

        while not exhausted and len(tasks) < limit:
            # next() on a generator runs the generator BODY, including the blocking S3 CSV
            # read — wrapping the to_thread call around the *call* that builds the
            # generator only builds the generator object and does no I/O, so the read would
            # happen back on the event loop. Wrapping next() itself keeps every blocking
            # read off the event loop while still pulling one row at a time.
            row = await asyncio.to_thread(_next_or_exhausted, it)
            if row is _EXHAUSTED:
                exhausted = True
                break
            tasks.add(asyncio.create_task(guarded(row)))

        if tasks:
            done_tasks, tasks = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED)
            drain(done_tasks, counters, label)

    counters.flush(force=True)


# ---------------------------------------------------------------- drive + recovery

async def drive(
    run_id: str,
    *,
    segment: str,
    phases: Callable[[str, store.Counters, dict[str, Any]], Awaitable[dict[str, Any]]],
    pipeline_label: str,
    search_label: str,
) -> None:
    """Run (or resume) one run end to end. Idempotent and never raises.

    `phases` does the pipeline-specific work — it receives (prefix, counters, pointer) and
    returns the summary that write_outputs produced. Everything around it (single-flight,
    counter carry-forward, Supabase status, failure terminalisation) is identical for
    every S3-only pipeline.

    Every phase skips work that already has an object, so calling this on a run that is
    partly or fully done costs no scrape.do credits and no Gemini tokens. That property
    only holds across SEPARATE drives, though — two drivers racing the SAME run at the same
    time can both see the same "pending" set and both submit it, which does re-spend.
    _driving is the single-flight guard against that.
    """
    if run_id in _driving:
        _LOGGER.info("%s run %s: already being driven in this process, skipping",
                     pipeline_label, run_id)
        return
    _driving.add(run_id)
    try:
        pointer = await asyncio.to_thread(store.read_run_pointer, run_id, segment)
        if not pointer:
            _LOGGER.warning("%s run %s: no pointer object, nothing to drive",
                            pipeline_label, run_id)
            return
        prefix = str(pointer.get("prefix") or "")
        status = await asyncio.to_thread(store.read_status, prefix) or {}
        # created_at is carried forward, never re-stamped: it is what makes total wall
        # clock survive a worker restart mid-run.
        counters = store.Counters(prefix, rows_total=int(status.get("rows_total") or 0),
                                  created_at=status.get("created_at"))
        for field in counters.values:
            counters.values[field] = int(status.get(field) or 0)

        # create_run stamps "queued" and the terminal write was the ONLY other update_run
        # call, so a healthy 6-day 500k run read "queued" in the Runs list for its whole
        # life. Best-effort, like every Supabase write in these pipelines.
        await asyncio.to_thread(update_supabase, pointer, status="running")

        try:
            summary = await phases(prefix, counters, pointer)
            await asyncio.to_thread(
                notify_terminal, run_id, pointer, summary,
                pipeline=pipeline_label, search_label=search_label)
        except Exception as exc:
            _LOGGER.exception("%s run %s failed: %s", pipeline_label, run_id, exc)
            # Counted so the re-drive scan can retry a bounded number of times — one
            # transient error must not park a multi-day run permanently.
            counters.bump(drive_attempts=1)
            counters.set_phase("failed")
            counters.flush(force=True)
            # Terminalize Supabase too, or the Runs list shows "queued" forever for a run
            # that is never coming back.
            await asyncio.to_thread(
                update_supabase, pointer, status="failed",
                finished_at=datetime.now(timezone.utc).isoformat())
    finally:
        _driving.discard(run_id)


def update_supabase(pointer: dict[str, Any], **fields: Any) -> None:
    """Best-effort partial update of this run's Supabase row. NEVER raises.

    Deliberately NOT routed through engine._update_supabase_run: that helper is rows-shaped
    for gsearch/firmographics (it rebuilds success/failed counts, websites_found/not_found,
    cost AND file_links from state["rows"]/state["upload_id"]), and these pipelines keep
    none of that — every one of those would come back zeroed or empty instead of using the
    numbers write_outputs already computed. update_run() itself is just a partial
    `.update(fields)`, so calling it directly with the real numbers is no larger a diff than
    fighting the shared helper's rows assumption, and it's correct.

    An S3-only run has no state dict, so run_db_id rides in the pointer; no id means
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


def notify_terminal(run_id: str, pointer: dict[str, Any], summary: dict[str, Any],
                    *, pipeline: str, search_label: str) -> None:
    """Best-effort Slack + Supabase at terminal status. Never raises — bookkeeping must not
    fail a run that already produced its outputs.

    "stopped" is a status the Runs list UI has no specific CSS rule for — confirmed safe:
    static/js/ui.js's statusBadge() renders any string as plain text with a data-status
    attribute, and app.css's unmatched-selector fallback is just the neutral base badge
    style, not blank/broken.
    """
    cost = summary.get("cost") or {}
    outcomes = summary.get("outcome_breakdown") or {}
    status = summary.get("status") or (
        "completed_with_errors" if outcomes.get("errored") else "completed")

    update_supabase(
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
            pipeline=pipeline,
            company=pointer.get("company_name"),
            run_ref=run_id,
            status=status,
            found=summary.get("websites_found"),
            not_found=summary.get("websites_not_found"),
            errored=outcomes.get("errored"),
            total_rows=summary.get("total_rows"),
            searches=cost.get("scrapedo_requests"),
            search_label=search_label,
            credits=cost.get("scrapedo_credits"),
        )
    except Exception:
        pass


def stale_seconds(env_key: str) -> int:
    return max(60, _get_int_env(env_key, 900))


def max_drive_attempts(env_key: str) -> int:
    return max(1, _get_int_env(env_key, 3))


def age_seconds(updated_at: Any) -> float:
    try:
        parsed = time.strptime(str(updated_at), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return float("inf")
    return max(0.0, time.time() - calendar.timegm(parsed))


def redrivable(status: dict[str, Any], max_attempts: int) -> bool:
    """Should the scan pick this run up?

    A run whose phase is "failed" is worth retrying: the driver died on an exception, and on
    a multi-day run that is usually transient. A re-drive costs nothing — every row with an
    object is skipped, so no scrape.do credits and no Gemini tokens — which is why "failed"
    used to be a dead end for no good reason: recovery meant hand-typing the run id into the
    Operations page. Bounded by drive_attempts so a genuinely broken run still stops instead
    of looping forever.
    """
    phase = str(status.get("phase") or "")
    if phase in TERMINAL_PHASES:
        return False
    if phase == "failed":
        return int(status.get("drive_attempts") or 0) < max_attempts
    return True


async def redrive_stale(
    *,
    segment: str,
    drive_fn: Callable[[str], Awaitable[None]],
    stale_sec: int,
    max_attempts: int,
) -> int:
    """Re-drive runs that stopped making progress. Returns how many were re-driven.

    This is the ONLY self-healing machinery these pipelines need, and it replaces
    ack-after-persist: a per-run message held for hours would hit RabbitMQ's 30-minute
    consumer_timeout and have its channel torn down, so the consumer acks on receipt
    instead. This scan also covers what a redelivery never could — the worker being down
    when the run was published, or the instance being replaced.

    Safe to run at any time: a re-drive skips every row that already has an object.
    """
    driven = 0
    for pointer in await asyncio.to_thread(store.list_run_pointers, segment):
        prefix = str(pointer.get("prefix") or "")
        run_id = str(pointer.get("run_id") or "")
        if not prefix or not run_id:
            continue
        status = await asyncio.to_thread(store.read_status, prefix) or {}
        if not redrivable(status, max_attempts):
            continue
        # The staleness gate applies to failed runs too, so a run that dies instantly cannot
        # hot-loop through its attempts in one scan pass.
        if age_seconds(status.get("updated_at")) < stale_sec:
            continue
        _LOGGER.info("%s %s: stale, re-driving", segment, run_id)
        await drive_fn(run_id)
        driven += 1
    return driven


def run_queues() -> list[tuple[str, str]]:
    """(queue, routing_key) for every S3-only pipeline. Imported lazily — these modules
    import this one."""
    from app.services.serpwow import firmographics_runner, gmaps_runner, relationship_runner

    return [
        (gmaps_runner.GMAPS_QUEUE, gmaps_runner.GMAPS_ROUTING_KEY),
        (relationship_runner.RELATIONSHIP_QUEUE, relationship_runner.RELATIONSHIP_ROUTING_KEY),
        (firmographics_runner.FIRMOGRAPHICS_QUEUE,
         firmographics_runner.FIRMOGRAPHICS_ROUTING_KEY),
    ]


async def declare_run_queues(channel, exchange) -> None:
    """Declare + bind the three run queues, so the PUBLISHER can create them too.

    Without this they existed only where ``consume_runs`` ran — the worker. The exchange is
    DIRECT, so a run published before the worker had ever bound its queue was silently
    DISCARDED by RabbitMQ: no error, no log, no message anywhere, and the run sat at
    phase="queued" until the stale-run scan noticed it up to GMAPS_STALE_SEC (900s) later.
    That window opens every time the broker's data volume is fresh, or the queues are
    deleted, and it is exactly the "upload right after starting everything" case.

    Declaring is idempotent with the worker's own declare (same name, same durability), so
    both may run in any order. Best-effort per queue: one pipeline's failure must not stop
    the others, and none of it may take the API down.
    """
    for queue_name, routing_key in run_queues():
        try:
            queue = await channel.declare_queue(queue_name, durable=True)
            await queue.bind(exchange, routing_key=routing_key)
        except Exception as exc:  # noqa: BLE001 - best-effort, mirrors the AI Mode init
            _LOGGER.warning("could not declare/bind %s: %s: %s",
                            queue_name, type(exc).__name__, exc)


async def consume_runs(channel, *, queue_name: str, routing_key: str,
                       drive_fn: Callable[[str], Awaitable[None]],
                       label: str) -> None:
    """Declare the run queue, bind it to the shared exchange, and start consuming.

    Own queue and channel, NOT the shared SerpWow queue: a run message occupies its consumer
    for the whole run, so on the shared queue it would permanently eat one of
    WORKER_CONCURRENCY's slots. Bound to the SAME direct exchange AI Mode uses
    (RABBITMQ_EXCHANGE, default "singleRA_search").
    """
    exchange = await channel.declare_exchange(
        os.getenv("RABBITMQ_EXCHANGE", "singleRA_search"),
        aio_pika.ExchangeType.DIRECT, durable=True)
    queue = await channel.declare_queue(queue_name, durable=True)
    await queue.bind(exchange, routing_key=routing_key)

    async def on_message(message) -> None:
        # Ack FIRST — see redrive_stale for why these pipelines invert the repo's usual
        # ack-after-persist rule.
        await message.ack()
        try:
            body = json.loads(message.body.decode("utf-8"))
        except Exception:
            _LOGGER.warning("%s: undecodable run message, dropped", label)
            return
        # body can be valid JSON that isn't an object (e.g. a bare list) — .get() on that
        # would raise AttributeError, so guard rather than let a malformed-but-parseable
        # message crash the consumer.
        run_id = str(body.get("run_id") or "") if isinstance(body, dict) else ""
        if run_id:
            await drive_fn(run_id)

    await queue.consume(on_message)
