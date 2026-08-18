# backend/app/services/serpwow/gmaps_runner.py
"""gmaps run orchestration: scrape -> outputs. No state.json, no local disk.

Two phases, not three: confidence is heuristic, so there is no LLM pass to run and
nothing to shard. That makes this the smallest S3-only pipeline in the repo — the generic
half (one message per run, the bounded task window, stop gating, the single-flight guard,
the stale-run re-drive, the consumer, the Supabase/Slack terminal write) all lives in
s3_run_driver and is shared with relationship.

Why it moved off state.json at all: update_row_state rewrote the WHOLE state file under a
per-upload lock for every row, so cost grew with the SQUARE of the row count — ~8GB of
rewrites at the documented 2.7k-row ceiling, ~275TB at 500k. Here a row costs two fixed
PUTs and no lock, so per-row cost no longer depends on how many rows came before it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.services.serpwow import s3_run_driver as driver
from app.services.serpwow import s3_run_store as store
from app.services.serpwow.gmaps_outputs import write_outputs
from app.services.serpwow.modes.gmaps import execute_gmaps_lookup, row_fields
from app.services.serpwow.row_logging import _log_row_stage

_LOGGER = logging.getLogger(__name__)

PIPELINE_SEGMENT = "gmaps"
GMAPS_QUEUE = "gmaps_runs"
GMAPS_ROUTING_KEY = "gmaps.run"

# A gmaps row is done when its COMPUTED result exists, not when its raw response does:
# raw/ is written first and rows/ second, so a crash between the two just re-scrapes.
DONE_PREFIX = "rows"


async def _scrape_one(prefix: str, row: dict[str, Any], counters: store.Counters) -> None:
    fields = row_fields(row)
    idx = int(fields["row_index"])

    if not fields["company_name"]:
        # One row in, one row out: a nameless row cannot be searched, but it still has to
        # appear in the outputs. Recorded as a result (not an error object) because
        # nothing failed — there was nothing to look up. Costs no credits.
        await asyncio.to_thread(
            store.put_object, store.row_key(prefix, idx),
            {"row_index": idx, "fields": fields, "context": None,
             "row_error": "Row has no company name."})
        counters.bump(rows_scraped=1)
        counters.flush()
        return

    response, raw_json = await execute_gmaps_lookup(
        company_name=fields["company_name"],
        country=fields["country"],
        firm_id=fields["firm_id"] or None,
        input_industry=fields["industry"] or None,
        input_full_address=fields["full_address"] or None,
        debug_upload_id=prefix.rsplit("/", 1)[-1],
        debug_row_index=idx,
    )
    context = dict(response.context or {})
    cost = context.get("cost_breakdown") or {}
    counters.bump(requests=int(cost.get("scrapedo_requests") or 0),
                  credits=int(cost.get("scrapedo_credits") or 0))

    try:
        # The provider's payload first — it is the artifact a human reads to judge a row,
        # and it must never be the thing that says "done".
        if raw_json:
            await asyncio.to_thread(
                store.put_bytes, store.raw_key(prefix, idx),
                raw_json.encode("utf-8"), "application/json")
        await asyncio.to_thread(
            store.put_object, store.row_key(prefix, idx),
            {"row_index": idx, "fields": fields, "context": context,
             "official_website": response.official_website or "",
             "summary": response.summary or ""})
    except Exception as exc:
        # S3 is the ONLY copy. A failed PUT means this row's work is gone, so it must
        # count as failed and be redone on the next drive — never silently swallowed.
        _LOGGER.warning("gmaps row %s: S3 put failed: %s: %s", idx, type(exc).__name__, exc)
        counters.bump(rows_failed=1)
        return

    if context.get("error"):
        counters.bump(rows_failed=1)
    else:
        counters.bump(rows_scraped=1)
    if int(cost.get("scrapedo_no_results") or 0):
        counters.bump(rows_no_listing=1)
    if int(cost.get("scrapedo_billed_empty") or 0):
        counters.bump(rows_billed_empty=1)
    counters.flush()

    # Per-row trace via the repo's existing helper, which PRINTS — _LOGGER alone leaves
    # `python worker.py` silent for a whole run (nothing configures a handler there).
    _log_row_stage(
        "gmaps.scrape",
        (f"company={fields['company_name']!r} -> "
         f"{response.official_website or 'not found'} "
         f"attempts={cost.get('scrapedo_requests')} credits={cost.get('scrapedo_credits')}"
         + (" no_listing=1" if cost.get("scrapedo_no_results") else "")
         + (f" error={context['error']}" if context.get("error") else "")),
        upload_id=prefix.rsplit("/", 1)[-1],
        row_index=idx,
        level="WARN" if context.get("error") else "INFO",
    )


async def run_scrape_phase(prefix: str, counters: store.Counters) -> None:
    """Scrape every row that has no result yet, `SCRAPEDO_CONCURRENCY` at a time."""
    await driver.run_row_phase(
        prefix, counters,
        lambda row: _scrape_one(prefix, row, counters),
        label="gmaps",
        done_prefix=DONE_PREFIX)


async def _phases(prefix: str, counters: store.Counters,
                  pointer: dict[str, Any]) -> dict[str, Any]:
    """Scrape, then the outputs. There is no phase 2 — heuristic confidence is computed
    inside the row task, so nothing is deferred and nothing is batched."""
    _phase = time.monotonic()
    await run_scrape_phase(prefix, counters)
    counters.bump(scrape_seconds=int(time.monotonic() - _phase))
    return await asyncio.to_thread(write_outputs, prefix, counters)


async def drive_run(run_id: str) -> None:
    """Run (or resume) one gmaps run end to end. Idempotent and never raises."""
    await driver.drive(
        run_id,
        segment=PIPELINE_SEGMENT,
        phases=_phases,
        pipeline_label="gmaps",
        search_label="Scrape.do searches",
    )


async def redrive_stale_runs() -> int:
    """Re-drive gmaps runs that stopped making progress (see s3_run_driver)."""
    return await driver.redrive_stale(
        segment=PIPELINE_SEGMENT,
        drive_fn=drive_run,
        stale_sec=driver.stale_seconds("GMAPS_STALE_SEC"),
        max_attempts=driver.max_drive_attempts("GMAPS_MAX_DRIVE_ATTEMPTS"),
    )


async def consume_gmaps_runs(channel) -> None:
    """Declare the gmaps run queue and start consuming (see s3_run_driver)."""
    await driver.consume_runs(
        channel,
        queue_name=GMAPS_QUEUE,
        routing_key=GMAPS_ROUTING_KEY,
        drive_fn=drive_run,
        label="gmaps",
    )
