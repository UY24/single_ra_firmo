# backend/app/services/serpwow/firmographics_runner.py
"""firmographics run orchestration: scrape -> (optional Gemini batch) -> outputs.

No state.json, no local disk. S3 object presence IS the row state, exactly like gmaps and
relationship, and for the same reason: ``update_row_state`` rewrote the WHOLE state file
under a per-upload lock on every row, so bytes written grew with the SQUARE of the row
count -- ~7.6GB of rewrites at the documented 2.7k-row ceiling, ~256TB at 500k. Here a row
costs a fixed two or three PUTs and no lock.

Three objects, three meanings:
  ``raw/<shard>/row_N.json``     the provider's SERP, verbatim off the wire
  ``rows/<shard>/row_N.json``    OUR result for the row + its cost. Phase-1 DONE marker
  ``cleaned/<shard>/row_N.json`` the six firmographic fields, when a Gemini BATCH produced
                                 them. Phase-2 DONE marker; absent in inline LLM mode

``rows/`` is written after ``raw/`` so a crash between the two just re-scrapes that row.
In INLINE LLM mode the fields are already in ``rows/`` and phase 2 finds nothing pending,
so it costs one LIST and returns -- the phase exists unconditionally rather than being
branched around, because which mode a run used has to be recoverable from the objects.

The generic half -- one message per run, the bounded task window, stop gating, the
single-flight guard, the stale-run re-drive, the consumer, the Supabase/Slack terminal
write -- is s3_run_driver, shared with gmaps and relationship.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.services.common import llm_batch
from app.services.serpwow import s3_run_driver as driver
from app.services.serpwow import s3_run_store as store
from app.services.serpwow.constants import PIPELINE_FIRMOGRAPHICS
from app.services.serpwow.firmographics_outputs import write_outputs
from app.services.serpwow.gemini_llm import build_ai_overview_prompt
from app.services.serpwow.modes.firmographics import (
    execute_firmographic_extraction,
    row_fields,
)
from app.services.serpwow.row_logging import _log_row_stage

_LOGGER = logging.getLogger(__name__)

PIPELINE_SEGMENT = "firmographics"
FIRMOGRAPHICS_QUEUE = "firmographics_runs"
FIRMOGRAPHICS_ROUTING_KEY = "firmographics.run"

# Phase 1 is done for a row when OUR result exists, not when the SERP does — raw/ is
# written first, so a crash in between re-scrapes rather than stranding a row.
DONE_PREFIX = "rows"

# The six fields a batch fills in. Kept here (not imported from modes) because the batch
# reply is parsed JSON, not a CrawlResponse.
_FIELD_KEYS = ("address", "phone", "email", "industry", "products", "services")


def _has_fields(fields: Any) -> bool:
    return isinstance(fields, dict) and any(fields.get(k) for k in _FIELD_KEYS)


# ------------------------------------------------------------------ phase 1: scrape

async def _scrape_one(prefix: str, row: dict[str, Any],
                      counters: store.Counters, batch_mode: bool) -> None:
    fields = row_fields(row)
    idx = int(fields["row_index"])

    if not fields["official_website"]:
        # One row in, one row out. A row with no website cannot be enriched, but it still
        # has to appear in the outputs. Recorded as a RESULT, not an error: nothing failed,
        # there was nothing to look up — and it costs no credits.
        await asyncio.to_thread(
            store.put_object, store.row_key(prefix, idx),
            {"row_index": idx, "fields": fields, "context": None, "result": None,
             "row_error": "Row has no website_url."})
        counters.bump(rows_scraped=1)
        counters.flush()
        return

    response, raw_json = await execute_firmographic_extraction(
        official_website=fields["official_website"],
        company_name=fields["company_name"],
        country=fields["country"],
        firm_id=fields["firm_id"] or None,
        input_industry=fields["industry"] or None,
        input_full_address=fields["full_address"] or None,
    )
    context = dict(response.context or {})
    cost = context.get("cost_breakdown") or {}
    counters.bump(requests=int(cost.get("scrapedo_requests") or 0),
                  credits=int(cost.get("scrapedo_credits") or 0))

    result = {key: getattr(response, key) for key in _FIELD_KEYS}
    scrapedo = context.get("scrapedo") if isinstance(context.get("scrapedo"), dict) else {}
    overview = scrapedo.get("ai_overview")
    # A batch can only fill a row that HAS an overview to normalise. Without one there is
    # nothing deferred, so the row is final now and phase 2 must not pay for it.
    awaiting = bool(batch_mode and isinstance(overview, dict) and overview)

    try:
        # The provider's payload first — it is the artifact a human reads to judge a row,
        # and it must never be the object that says "done".
        if raw_json:
            await asyncio.to_thread(
                store.put_bytes, store.raw_key(prefix, idx),
                raw_json.encode("utf-8"), "application/json")
        await asyncio.to_thread(
            store.put_object, store.row_key(prefix, idx),
            {"row_index": idx, "fields": fields, "context": context,
             "result": None if awaiting else result,
             "official_website": response.official_website or "",
             "summary": response.summary or "",
             "gemini_cost_usd": float(response.gemini_cost_usd or 0.0),
             "awaiting_batch": awaiting})
        if awaiting:
            # A tiny marker so phase 2 can find its work with ONE list instead of a GET
            # per row. Written AFTER rows/, so it can never point at a row that has none.
            await asyncio.to_thread(
                store.put_object, store.pending_llm_key(prefix, idx), {"row_index": idx})
    except Exception as exc:
        # S3 is the ONLY copy. A failed PUT means this row's work is gone, so it counts as
        # failed and gets redone on the next drive — never silently swallowed.
        _LOGGER.warning("firmographics row %s: S3 put failed: %s: %s",
                        idx, type(exc).__name__, exc)
        counters.bump(rows_failed=1)
        return

    if context.get("formatted_results"):
        counters.bump(rows_failed=1)
    else:
        counters.bump(rows_scraped=1)
    if int(cost.get("scrapedo_billed_empty") or 0):
        counters.bump(rows_billed_empty=1)
    counters.flush()

    _log_row_stage(
        "firmographics.scrape",
        (f"site={fields['official_website']!r} "
         f"overview={'yes' if isinstance(overview, dict) and overview else 'no'} "
         f"deferred={int(awaiting)} "
         f"attempts={cost.get('scrapedo_requests')} credits={cost.get('scrapedo_credits')}"),
        upload_id=prefix.rsplit("/", 1)[-1],
        row_index=idx,
        level="WARN" if context.get("formatted_results") else "INFO",
    )


async def run_scrape_phase(prefix: str, counters: store.Counters) -> None:
    """Scrape every row with no result yet, `SCRAPEDO_CONCURRENCY` at a time."""
    batch_mode = llm_batch.batch_enabled(PIPELINE_FIRMOGRAPHICS)
    await driver.run_row_phase(
        prefix, counters,
        lambda row: _scrape_one(prefix, row, counters, batch_mode),
        label="firmographics",
        done_prefix=DONE_PREFIX)


# ------------------------------------------------------------- phase 2: Gemini batch

def _batch_item(idx: int, stored: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """One Gemini Batch request for a scraped row, using the SHARED prompt builder so a
    batched run cannot answer a row differently from an inline one."""
    fields = stored.get("fields") or {}
    scrapedo = stored.get("context", {}).get("scrapedo") or {}
    overview = scrapedo.get("ai_overview")
    prompt = build_ai_overview_prompt(
        str(fields.get("company_name") or ""),
        str(fields.get("country") or ""),
        str(stored.get("official_website") or ""),
        overview if isinstance(overview, dict) else {})
    return str(idx), {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
    }


def _run_gemini_batch(prefix: str, items: list[tuple[str, dict]],
                      counters: store.Counters | None = None) -> dict[str, dict]:
    """Submit one shard and block until terminal; returns {key: {parsed, usage, model}}.

    Seam for tests. Sync on purpose — callers wrap it in ``asyncio.to_thread``.

    The job NAME is recorded in S3 before the first poll: a Gemini batch runs on Google's
    side and is billed whether or not we are still listening, so losing the name (restart,
    timeout, crash) would mean paying for the shard twice. Cleared by the caller once the
    fields are durable, not here — a crash in between would lose both.
    """
    from app.services.ai_mode import gemini_batch as gb

    if not items:
        return {}
    model = llm_batch.batch_model()
    created = gb.create_batch(model, items, display_name=f"firmographics-{prefix}")
    name = gb.batch_name_from_create(created)
    indices = sorted(int(key) for key, _body in items)
    try:
        store.put_object(store.batch_record_key(prefix, indices),
                         {"name": name, "model": model, "indices": indices})
    except Exception as exc:
        # Best-effort, unlike every other write here: raising would abandon a batch Google
        # is already billing us for — the exact double-spend the record prevents. This
        # process still polls on; only crash-recovery is lost.
        _LOGGER.warning("firmographics %s: could not record batch %s (%s: %s) — a restart "
                        "before it resolves will resubmit these rows",
                        prefix, name, type(exc).__name__, exc)
    obj = _poll_to_terminal(gb, name, counters)
    _abort_if_shard_died(gb, obj, store.batch_record_key(prefix, indices), name)
    return _collect(gb, obj, model)


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


def _poll_to_terminal(gb, name: str, counters: store.Counters | None):
    """Block until this job is terminal. Bounded by GEMINI_BATCH_TIMEOUT_SEC (48h).

    ``counters`` is force-flushed every poll — a heartbeat. Without it a run legitimately
    waiting hours on one batch writes status.json once and goes silent, which the stale-run
    re-drive reads as dead and starts a second, re-spending drive on a healthy run.
    """
    import time as _time

    deadline = _time.monotonic() + llm_batch.timeout_sec()
    while True:
        obj = gb.get_batch(name)
        if gb.is_terminal(gb.state_name(obj), bool(obj.get("done"))):
            return obj
        if _time.monotonic() >= deadline:
            # Raise rather than return {}: the rows keep no cleaned/ object AND the batch
            # record survives, so the next drive RE-ATTACHES to this same job instead of
            # paying for a second one.
            raise TimeoutError(f"Gemini batch {name} did not finish within "
                               f"GEMINI_BATCH_TIMEOUT_SEC")
        if counters is not None:
            counters.flush(force=True)
        _time.sleep(llm_batch.poll_sec())


def _collect(gb, obj: dict, model: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for record in gb.collect_results(obj):
        key = str(record.get("key") or "")
        if not key:
            continue
        try:
            parsed = json.loads(record.get("text") or "")
        except Exception:
            parsed = None
        out[key] = {"parsed": parsed if isinstance(parsed, dict) else None,
                    "usage": record.get("usage"), "model": model}
    return out


async def _write_fields(prefix: str, keys: list[str], results: dict[str, dict],
                        counters: store.Counters) -> None:
    """Persist one shard's answers. A key with no answer still gets an object, or a re-drive
    resubmits (and re-pays for) a shard Google already answered badly."""
    for key in keys:
        idx = int(key)
        got = results.get(key) or {}
        parsed = got.get("parsed")
        fields = ({k: parsed.get(k) for k in _FIELD_KEYS}
                  if isinstance(parsed, dict) else None)
        if isinstance(fields, dict):
            fields["products"] = fields.get("products") or []
            fields["services"] = fields.get("services") or []
        await asyncio.to_thread(
            store.put_object, store.cleaned_key(prefix, idx),
            {"row_index": idx, "result": fields, "usage": got.get("usage") or {},
             "model": got.get("model") or "",
             "error": None if parsed is not None else "Gemini batch returned no answer."})
        counters.bump(rows_cleaned=1)
    counters.flush()


async def _reattach_batches(prefix: str, counters: store.Counters) -> None:
    """Finish any shard a previous drive paid for but never collected.

    Runs BEFORE this drive computes its pending set, so those rows get their answers from
    the job Google already ran instead of being resubmitted.
    """
    from app.services.ai_mode import gemini_batch as gb

    records = await asyncio.to_thread(store.list_batch_records, prefix)
    if not records:
        return
    cleaned = await asyncio.to_thread(store.list_cleaned_rows, prefix)
    for record in records:
        pending = [int(i) for i in (record.get("indices") or []) if int(i) not in cleaned]
        if not pending:
            # Stale record (its rows landed anyway) — free to clear.
            await asyncio.to_thread(store.delete_object, record["record_key"])
            continue
        _log_row_stage("firmographics.phase",
                       f"LLM phase: re-attaching to Gemini batch {record['name']} "
                       f"({len(pending)} row(s) still without fields)",
                       upload_id=prefix.rsplit("/", 1)[-1])
        obj = await asyncio.to_thread(
            _poll_to_terminal, gb, str(record["name"]), counters)
        await asyncio.to_thread(
            _abort_if_shard_died, gb, obj, record["record_key"], str(record["name"]))
        results = await asyncio.to_thread(
            _collect, gb, obj, str(record.get("model") or ""))
        await _write_fields(prefix, [str(i) for i in pending], results, counters)
        await asyncio.to_thread(store.delete_object, record["record_key"])


async def run_llm_phase(prefix: str, counters: store.Counters) -> None:
    """Fill the six fields for every row whose scrape deferred them to a batch.

    A no-op in inline mode: nothing is marked ``awaiting_batch``, so the pending set is
    empty and this costs one LIST. Deliberately not branched around by the mode flag —
    which mode a run used must be recoverable from the objects, not from current env.
    """
    counters.set_phase("cleaning")
    counters.flush(force=True)

    # Before computing pending: collect shards a previous drive already paid Google for.
    await _reattach_batches(prefix, counters)

    # Two LISTs, not one GET per row: in inline mode nothing is marked pending, so this
    # phase costs exactly one list and returns — which is what keeps a 500k inline run from
    # paying 500k round-trips to discover it has no LLM work.
    deferred = await asyncio.to_thread(store.list_pending_llm_rows, prefix)
    cleaned = await asyncio.to_thread(store.list_cleaned_rows, prefix)
    pending = sorted(deferred - cleaned)
    if not pending:
        counters.flush(force=True)
        return

    _log_row_stage("firmographics.phase",
                   f"LLM phase: {len(pending)} candidate row(s), "
                   f"shard_size={llm_batch.shard_size()} "
                   f"max_inflight={llm_batch.max_inflight()}",
                   upload_id=prefix.rsplit("/", 1)[-1])

    shard: list[tuple[str, dict]] = []
    inflight: set[asyncio.Task] = set()
    stopped = False

    async def submit(items: list[tuple[str, dict]]) -> None:
        results = await asyncio.to_thread(_run_gemini_batch, prefix, items, counters)
        keys = [key for key, _body in items]
        await _write_fields(prefix, keys, results, counters)
        # Only now that the fields are durable is the in-flight record safe to clear.
        if keys:
            await asyncio.to_thread(
                store.delete_object,
                store.batch_record_key(prefix, sorted(int(k) for k in keys)))

    for idx in pending:
        # Only rows the scrape phase MARKED are opened, so this GET is paid once per
        # genuinely deferred row rather than once per row in the run.
        stored = await asyncio.to_thread(store.get_object, store.row_key(prefix, idx))
        if not stored:
            continue     # marker outlived its row (a failed rows/ PUT): re-scrape will fix
        shard.append(_batch_item(idx, stored))
        if len(shard) >= llm_batch.shard_size():
            # Stop check at the SHARD boundary, checked BEFORE submitting: this is the only
            # thing between a stop at row 1 of a 500k run and buying every remaining shard.
            if await asyncio.to_thread(store.stop_requested, prefix):
                _LOGGER.info("firmographics %s: stop requested, submitting no more shards",
                             prefix)
                stopped = True
                break
            while len(inflight) >= llm_batch.max_inflight():
                finished, inflight = await asyncio.wait(
                    inflight, return_when=asyncio.FIRST_COMPLETED)
                driver.drain(finished, counters, "llm")
            inflight.add(asyncio.create_task(submit(shard)))
            shard = []

    if shard and not stopped:
        inflight.add(asyncio.create_task(submit(shard)))
    if inflight:
        finished, _ = await asyncio.wait(inflight)
        driver.drain(finished, counters, "llm")
    counters.flush(force=True)


# ------------------------------------------------------------------------ orchestration

async def _phases(prefix: str, counters: store.Counters,
                  pointer: dict[str, Any]) -> dict[str, Any]:
    """Scrape, then the LLM pass, then the outputs."""
    _phase = time.monotonic()
    await run_scrape_phase(prefix, counters)
    counters.bump(scrape_seconds=int(time.monotonic() - _phase))
    # A stop does NOT discard the run: skip only the money-spending LLM phase.
    # write_outputs handles rows with no fields fine, and it is what labels the run
    # "stopped", writes the CSVs and gives the terminal notify something to send.
    if not await asyncio.to_thread(store.stop_requested, prefix):
        _phase = time.monotonic()
        await run_llm_phase(prefix, counters)
        counters.bump(llm_seconds=int(time.monotonic() - _phase))
    return await asyncio.to_thread(write_outputs, prefix, counters)


async def drive_run(run_id: str) -> None:
    """Run (or resume) one firmographics run end to end. Idempotent, never raises."""
    await driver.drive(
        run_id,
        segment=PIPELINE_SEGMENT,
        phases=_phases,
        pipeline_label="firmographics",
        search_label="Scrape.do searches",
    )


async def redrive_stale_runs() -> int:
    """Re-drive runs that stopped making progress (see s3_run_driver)."""
    return await driver.redrive_stale(
        segment=PIPELINE_SEGMENT,
        drive_fn=drive_run,
        stale_sec=driver.stale_seconds("FIRMOGRAPHICS_STALE_SEC"),
        max_attempts=driver.max_drive_attempts("FIRMOGRAPHICS_MAX_DRIVE_ATTEMPTS"),
    )


async def consume_firmographics_runs(channel) -> None:
    """Declare the run queue and start consuming (see s3_run_driver)."""
    await driver.consume_runs(
        channel,
        queue_name=FIRMOGRAPHICS_QUEUE,
        routing_key=FIRMOGRAPHICS_ROUTING_KEY,
        drive_fn=drive_run,
        label="firmographics",
    )
