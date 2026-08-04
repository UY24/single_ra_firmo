# backend/app/services/serpwow/relationship_runner.py
"""Relationship run orchestration: scrape -> verdict -> outputs.

ONE RabbitMQ message carries a whole run. The message body is just {"run_id": ...}; the
rows live in input.csv on S3, and parallelism comes from the bounded task window in here
(topped up to RELATIONSHIP_CONCURRENCY and refilled as tasks finish), not from the
message count. A single async process in this repo has already held 60 concurrent
scrape.do calls, so the broker is a durable start signal, not a work distributor.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services.common.env import get_int_env as _get_int_env
from app.services.serpwow import relationship_store as store
from app.services.serpwow.modes.relationship import build_evidence, row_fields
from app.services.serpwow.query_builders import build_relationship_search_query
from app.services.serpwow.scrapedo_ai_client import search_ai_mode
from app.services.serpwow.url_utils import x_domain_from_input_url

_LOGGER = logging.getLogger(__name__)


def _concurrency() -> int:
    """In-flight AI Mode calls for this pipeline.

    Defaults to 100 per the spec. NOT proven: on scrape.do's Maps endpoint this repo
    measured 100 running 1.9x SLOWER than 25, because scrape.do queues rather than
    429ing. Re-measure on a 100-row run before trusting this number.
    """
    return max(1, _get_int_env("RELATIONSHIP_CONCURRENCY", 100))


async def _scrape_one(prefix: str, row: dict[str, Any], counters: store.Counters) -> None:
    fields = row_fields(row)
    idx = int(fields["row_index"])
    x_domain = x_domain_from_input_url(fields["input_url"])
    query = build_relationship_search_query(
        x_name=fields["x_name"], y_name=fields["y_name"], x_domain=x_domain,
        input_url=fields["input_url"], city=fields["city"], country=fields["country"])

    envelope = await search_ai_mode(query, gl=(fields["country"] or "us").lower()[:2])
    envelope["row_index"] = idx
    envelope["fields"] = fields
    envelope["x_domain"] = x_domain

    counters.bump(requests=int(envelope.get("request_count") or 0),
                  credits=int(envelope.get("credits") or 0))

    # A row that died after every retry gets an error marker, so a re-drive knows not to
    # retry it for free forever, and "Rerun failed" can find it.
    key = store.error_key(prefix, idx) if envelope.get("error") else store.raw_key(prefix, idx)
    try:
        await asyncio.to_thread(store.put_object, key, envelope)
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


async def run_scrape_phase(prefix: str, counters: store.Counters) -> None:
    """Scrape every row that has no object yet, `RELATIONSHIP_CONCURRENCY` at a time."""
    counters.set_phase("scraping")
    counters.flush(force=True)

    done = await asyncio.to_thread(store.list_done_rows, prefix)
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

    while not exhausted or tasks:
        # Checked once per refill of the task window, not once per row: at 500k rows a
        # per-row S3 GET here would serialize ~500k round-trips into the dispatch loop
        # that gates how fast new work starts.
        if not exhausted and await asyncio.to_thread(store.stop_requested, prefix):
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
            _, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

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
