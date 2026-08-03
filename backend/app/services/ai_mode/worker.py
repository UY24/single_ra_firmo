# backend/app/services/ai_mode/worker.py
"""AI Mode's broker worker: consume scrape-batch jobs, enforce the phase barrier.

One message = one scrape.do batch (see broker.py for the queue topology). The
durable per-batch state is FILE PRESENCE, not a state dict:
  * scraped ......... raw_responses/request_NNNNNN.json parses
  * failed (terminal) raw_responses/request_NNNNNN.error.json exists
  * cleaned ......... cleaned/batch-NNNNNN.json parses
status.json carries only O(1) counters (single-writer: this worker owns it from
first publish until terminal). Counters are a cache — the Phase 1 -> 2 barrier
always recounts the directory before flipping to "cleaning" and dispatching
run_ai_mode_finish (registry-guarded so exactly one finish task runs per run).

Ack policy mirrors the SerpWow consumer: ack only AFTER the raw/error file is
durably on disk; a scrape.do API failure is a RESULT (error marker + ack), while
infrastructure crashes get one redelivery and are then terminalized with an
error marker so the barrier still resolves (no wedged runs).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from dataclasses import asdict

from app.models.entities import Entity, parse_entities_csv
from app.services.common.provider_limits import scrapedo_slot
from app.services.ai_mode import broker, run_store
from app.services.ai_mode import ai_mode_service as svc
from app.services.ai_mode.ai_mode_service import (
    RAW_RESPONSES_DIRNAME,
    _float_env,
    _int_env,
    _read_status,
    _persist_status,
    build_ai_mode_settings,
    sanitize_secret_text,
)
from app.services.ai_mode.mode_config import get_mode
from app.services.ai_mode.models import utc_now_iso

_LOG = logging.getLogger("ai_mode.worker")

# Per-run coordination (single worker process — same constraint as SerpWow's
# gemini_batch_tasks registry).
_run_locks: dict[str, asyncio.Lock] = {}
_finish_tasks: dict[str, asyncio.Task] = {}
_last_flush: dict[str, float] = {}

_stop_event: Optional[asyncio.Event] = None
_consumer_tasks: list[asyncio.Task] = []


def _reset_for_tests() -> None:
    """Clear per-run in-memory coordination state (tests only)."""
    _run_locks.clear()
    _finish_tasks.clear()
    _last_flush.clear()


def get_run_lock(run_id: str) -> asyncio.Lock:
    lock = _run_locks.get(run_id)
    if lock is None:
        lock = _run_locks.setdefault(run_id, asyncio.Lock())
    return lock


# --------------------------------------------------------------------------- #
# File-presence state
# --------------------------------------------------------------------------- #
def error_marker_path(run_dir: Path, request_index: int) -> Path:
    return run_dir / RAW_RESPONSES_DIRNAME / f"request_{request_index:06d}.error.json"


def raw_file_path(run_dir: Path, request_index: int) -> Path:
    return run_dir / RAW_RESPONSES_DIRNAME / f"request_{request_index:06d}.json"


def _scan_indices(run_dir: Path) -> tuple[set[int], set[int]]:
    """One directory scan -> (raw_indices, error_indices), deduped by index."""
    raw_idx: set[int] = set()
    err_idx: set[int] = set()
    raw_dir = run_dir / RAW_RESPONSES_DIRNAME
    try:
        with os.scandir(raw_dir) as it:
            for entry in it:
                name = entry.name
                if not (name.startswith("request_") and name.endswith(".json")):
                    continue
                digits = name[len("request_"):].split(".", 1)[0]
                try:
                    idx = int(digits)
                except ValueError:
                    continue
                if name.endswith(".error.json"):
                    err_idx.add(idx)
                else:
                    raw_idx.add(idx)
    except OSError:
        return set(), set()
    return raw_idx, err_idx


def count_done_batches(run_dir: Path) -> tuple[int, int]:
    """(raw_count, effective_error_count) — the barrier's truth.

    Counts DISTINCT indices (a batch with both a raw file and a stale error
    marker is one done batch, raw wins), so raw+err == len(raw ∪ err) and the
    barrier can never be satisfied by a double-counted index while another
    batch is genuinely missing.
    """
    raw_idx, err_idx = _scan_indices(run_dir)
    return len(raw_idx), len(err_idx - raw_idx)


def write_error_marker(
    run_dir: Path, mode_key: str, request_index: int, error: str,
    *, seconds: float = 0.0, note: str | None = None,
) -> None:
    """Terminalize one batch's scrape as failed (durable, S3-mirrored).

    A raw success file always wins — never overwrite/shadow one.
    """
    if raw_file_path(run_dir, request_index).exists():
        return
    record = {
        "request_index": request_index,
        "error": sanitize_secret_text(error),
        "seconds": round(seconds, 3),
        "at": utc_now_iso(),
    }
    if note:
        record["note"] = note
    path = error_marker_path(run_dir, request_index)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    except OSError:
        _LOG.warning("failed to write error marker %s", path, exc_info=True)
        return
    try:
        from app.services.ai_mode import s3_sync

        s3_sync.mirror_file_to_s3(run_dir, mode_key, path)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Producer: publish a run's batches (upload endpoint + resume republish)
# --------------------------------------------------------------------------- #
async def publish_run_batches(run_id: str, *, only_missing: bool = False) -> int:
    """Publish one scrape message per batch (+ one trailing check message).

    NEVER raises — the router fire-and-forgets this as a background task, so an
    escaped exception would strand the run in queued/failed with no error while
    the endpoint already answered 200. A crash here marks the run failed
    (status.json + Supabase), preserving the old engine's never-raises contract.

    The pre-publish status stamp is the API side's LAST status.json write —
    single-writer rule: the worker owns status.json from the first publish on.
    Individual publish failures are tolerated (logged, skipped): the reconciler
    republishes any batch with no raw/error file. ``only_missing`` skips batches
    that already have a raw or error file (resume republish).
    Returns the number of scrape messages published (0 on crash).
    """
    try:
        return await _publish_run_batches(run_id, only_missing=only_missing)
    except Exception as exc:
        _LOG.exception("publish_run_batches crashed for %s", run_id)
        try:
            run_dir = run_store.find_run_dir(run_id)
            if run_dir is not None:
                status = _read_status(run_id, run_dir)
                status["status"] = "failed"
                status["error"] = sanitize_secret_text(f"publish failed: {exc}")
                status["updated_at"] = utc_now_iso()
                _persist_status(run_id, run_dir, status)
                await asyncio.to_thread(
                    svc._supabase_update_run, status.get("run_db_id"),
                    status="failed", error=status["error"],
                    finished_at=status["updated_at"],
                )
        except Exception:
            _LOG.warning("failed to mark run %s failed after publish crash",
                         run_id, exc_info=True)
        return 0


async def _publish_run_batches(run_id: str, *, only_missing: bool = False) -> int:
    run_dir = run_store.find_run_dir(run_id)
    if run_dir is None:
        _LOG.error("publish_run_batches: run dir not found for %s", run_id)
        return 0
    status = _read_status(run_id, run_dir)
    mode = get_mode(str(status.get("mode") or "ai_bulk"))
    try:
        batch_size = int(status.get("batch_size") or 0) or mode.batch_size()
    except (TypeError, ValueError):
        batch_size = mode.batch_size()
    # CSV parse + grouping is pure CPU — a 1M-row file takes seconds; keep it
    # off the event loop (this runs as a background task on the API process).
    groups = await asyncio.to_thread(_load_groups, run_dir, batch_size)
    started_at = utc_now_iso()

    status["status"] = "running"
    status["engine"] = "broker"
    status["phase"] = "publishing"
    status["batches_total"] = len(groups)
    status["started_at"] = str(status.get("started_at") or started_at)
    status["error"] = None
    status["updated_at"] = started_at
    _persist_status(run_id, run_dir, status)
    await asyncio.to_thread(
        svc._supabase_update_run,
        status.get("run_db_id"), status="running", started_at=status["started_at"],
    )
    # Seed the S3 write-through (input.csv + the stamped status.json) so a hard
    # kill mid-scrape on an ephemeral host still leaves a resumable copy in S3
    # — raw/cleaned files are mirrored as they land, but only if these exist.
    from app.services.ai_mode import s3_sync

    mode_key = mode.key
    for seed in ("input.csv", "status.json"):
        await asyncio.to_thread(
            s3_sync.mirror_file_to_s3, run_dir, mode_key, run_dir / seed
        )

    company_name = str(status.get("company_name") or "")
    published = 0
    for idx, group in enumerate(groups, start=1):
        if only_missing and (
            await asyncio.to_thread(_raw_parseable, run_dir, idx)
            or error_marker_path(run_dir, idx).exists()
        ):
            continue
        payload = {
            "type": "scrape",
            "run_id": run_id,
            "request_index": idx,
            "batches_total": len(groups),
            "mode": mode.key,
            "company_name": company_name,
            "entities": [asdict(e) for e in group],
            "published_at": utc_now_iso(),
        }
        try:
            await broker.publish_scrape_job(payload)
            published += 1
        except Exception:
            _LOG.warning(
                "publish failed for run %s batch %s (reconciler will republish)",
                run_id, idx, exc_info=True,
            )
    try:
        # Kick a completion check (covers tiny runs and resumes with nothing to
        # re-scrape, where no further scrape ack would trigger the barrier).
        await broker.publish_check(run_id)
    except Exception:
        _LOG.warning("publish check failed for run %s", run_id, exc_info=True)
    _LOG.info("published %s/%s scrape batch(es) for run %s", published, len(groups), run_id)
    return published


def _load_groups(run_dir: Path, batch_size: int) -> list[list[Entity]]:
    """Parse input.csv and chunk into scrape batches (CPU-bound; call in a thread)."""
    raw_csv = (run_dir / "input.csv").read_bytes()
    return list(svc.chunked(parse_entities_csv(raw_csv).entities, batch_size))


def reset_run_for_resume(run_id: str, run_dir: Path) -> int:
    """API-side resume prep (call BEFORE republishing missing batches).

    Deletes terminal ``*.error.json`` markers so those batches are retried
    (raw and cleaned/ checkpoints are untouched — no scrape.do or LLM re-spend
    on successes), best-effort deletes each marker's S3 mirror (so a later
    rehydrate can't resurrect it), and clears stale batch bookkeeping
    (``gemini_batch_jobs``/``requeue_attempts``). Returns markers cleared.
    """
    from app.services.ai_mode import s3_sync

    status = _read_status(run_id, run_dir)
    mode_key = str(status.get("mode") or "ai_bulk")
    raw_dir = run_dir / RAW_RESPONSES_DIRNAME
    cleared = 0
    if raw_dir.is_dir():
        for path in sorted(raw_dir.glob("request_*.error.json")):
            try:
                path.unlink()
            except OSError:
                continue
            cleared += 1
            s3_sync.delete_mirrored_file(run_dir, mode_key, path)
    # A cleaned checkpoint whose text is not a parseable JSON array would error
    # the same batch on every resume (finish skips re-cleaning existing files) —
    # clear it so the batch is re-cleaned instead of failing forever.
    from app.services.ai_mode.cleanup import parse_json_array_from_text

    cleaned_dir = run_dir / svc.CLEANED_DIRNAME
    if cleaned_dir.is_dir():
        for path in sorted(cleaned_dir.glob("batch-*.json")):
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                text = str(obj.get("text") or "") if isinstance(obj, dict) else ""
            except (ValueError, OSError):
                text = ""
            if text and parse_json_array_from_text(text) is not None:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            s3_sync.delete_mirrored_file(run_dir, mode_key, path)
    status["gemini_batch_jobs"] = []
    status["requeue_attempts"] = {}
    status["delivery_failures"] = {}
    status["error"] = None
    # Flip out of the resumable statuses IMMEDIATELY: a second resume request
    # racing this one must hit the endpoint's 409 gate, not double-publish
    # (and double-bill) the missing batches.
    status["status"] = "running"
    status["phase"] = "publishing"
    status["updated_at"] = utc_now_iso()
    _persist_status(run_id, run_dir, status)
    return cleared


# --------------------------------------------------------------------------- #
# Job processing
# --------------------------------------------------------------------------- #
def _resolve_run_dir(payload: dict[str, Any]) -> Path | None:
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        return None
    run_dir = run_store.find_run_dir(run_id)
    if run_dir is None and str(payload.get("company_name") or "").strip():
        # Shared-disk contract: normally the API created it; recreate defensively.
        run_dir = run_store.run_dir_for(str(payload["company_name"]), run_id)
    return run_dir


async def process_scrape_job(payload: dict[str, Any]) -> None:
    """Process one AI-Mode message (scrape batch or completion check)."""
    run_id = str(payload.get("run_id") or "")
    run_dir = _resolve_run_dir(payload)
    if run_dir is None:
        _LOG.warning("ai_mode job for unknown run %r dropped", run_id)
        return

    if str(payload.get("type") or "scrape") == "check":
        await _completion_check(run_id, run_dir)
        return

    request_index = int(payload.get("request_index") or 0)
    if request_index <= 0:
        return
    # Idempotency: an existing terminal error marker means this batch already
    # failed for good (resume deletes markers before republishing).
    if (error_marker_path(run_dir, request_index).exists()
            and not raw_file_path(run_dir, request_index).exists()):
        await _completion_check_if_due(run_id, run_dir, payload)
        return

    group = [Entity(**e) for e in (payload.get("entities") or [])]
    mode = get_mode(str(payload.get("mode") or "ai_bulk"))
    settings = build_ai_mode_settings()
    scrapedo_client = svc.ScrapeDoClient(
        token=settings.scrapedo_token,
        timeout_seconds=settings.scrapedo_timeout_seconds,
        max_retries=settings.scrapedo_max_retries,
        device=settings.scrapedo_device,
        hl=settings.scrapedo_hl,
        gl=settings.scrapedo_gl,
        google_domain=settings.scrapedo_google_domain,
        safe=settings.scrapedo_safe,
        include_html=settings.scrapedo_include_html,
        log=lambda message: _LOG.debug("[run:%s] %s", run_id, sanitize_secret_text(message)),
    )
    # scrape_batch_sync is idempotent (reuses a parseable raw file) and never
    # raises for a scrape.do failure — that comes back as ok=False.
    started_at = utc_now_iso()
    scrape_t0 = time.perf_counter()
    svc._ai_log(
        run_id, run_dir,
        f"scrape batch {request_index} started started_at={started_at} "
        f"entities={len(group)}",
    )
    # Account-wide scrape.do gate, shared with the gmaps pipeline (both run in this
    # worker process against the same account, so two separate caps could sum past it).
    # Held across the to_thread call because that IS the HTTP request.
    async with scrapedo_slot():
        rec = await asyncio.to_thread(
            svc.scrape_batch_sync, run_dir, mode, settings, scrapedo_client,
            request_index, group,
        )
    finished_at = utc_now_iso()
    status_word = "success" if rec["ok"] else "error"
    if rec.get("reused"):
        status_word = "reused"
    svc._ai_log(
        run_id, run_dir,
        f"scrape batch {request_index} finished status={status_word} "
        f"started_at={started_at} finished_at={finished_at} "
        f"duration={time.perf_counter() - scrape_t0:.3f}s",
        logging.ERROR if not rec["ok"] else logging.INFO,
    )
    if not rec["ok"]:
        # A scrape.do failure is a RESULT (SerpWow-parity taxonomy): terminalize
        # durably so the barrier can resolve; Phase 3 emits error rows for it.
        await asyncio.to_thread(
            write_error_marker, run_dir, mode.key, request_index,
            rec["error"] or "scrape.do error", seconds=rec["scrapedo_seconds"],
        )
    await _after_job(run_id, run_dir, rec, len(group), payload)


async def _after_job(
    run_id: str, run_dir: Path, rec: dict, group_size: int, payload: dict,
) -> None:
    """Fold one processed job into the run counters; flip the barrier when due."""
    flush_sec = max(0.0, _float_env("AI_MODE_STATUS_FLUSH_SEC", 2.0))
    batches_total = int(payload.get("batches_total") or 0)
    due = False
    async with get_run_lock(run_id):
        status = _read_status(run_id, run_dir)
        if str(status.get("phase") or "") == "publishing":
            # First processed job: the run is visibly scraping now.
            status["phase"] = "scraping"
        if not rec.get("reused"):
            status["batches_done"] = int(status.get("batches_done") or 0) + 1
            status["scrapedo_request_count"] = int(status.get("scrapedo_request_count") or 0) + 1
            status["scrapedo_seconds_total"] = round(
                float(status.get("scrapedo_seconds_total") or 0.0)
                + float(rec.get("scrapedo_seconds") or 0.0), 3)
            if not rec["ok"]:
                status["scrapedo_failed_requests"] = int(status.get("scrapedo_failed_requests") or 0) + 1
                status["entities_without_scrape_data"] = (
                    int(status.get("entities_without_scrape_data") or 0) + group_size)
        status["updated_at"] = utc_now_iso()
        total = batches_total or int(status.get("batches_total") or 0)
        done_estimate = int(status.get("batches_done") or 0)
        now_mono = time.monotonic()
        if (flush_sec <= 0.0
                or now_mono - _last_flush.get(run_id, 0.0) >= flush_sec
                or (total and done_estimate >= total)):
            _persist_status(run_id, run_dir, status)
            _last_flush[run_id] = now_mono
        due = bool(total and done_estimate >= total)
    if due:
        await _completion_check(run_id, run_dir)


async def _completion_check_if_due(run_id: str, run_dir: Path, payload: dict) -> None:
    total = int(payload.get("batches_total") or 0)
    if not total:
        await _completion_check(run_id, run_dir)
        return
    raw, err = await asyncio.to_thread(count_done_batches, run_dir)
    if raw + err >= total:
        await _completion_check(run_id, run_dir)


async def _completion_check(run_id: str, run_dir: Path) -> bool:
    """Recount the directory (the truth); flip scraping -> cleaning at the barrier.

    Returns True when the flip happened (finish dispatched by the winner).
    """
    dispatch = False
    async with get_run_lock(run_id):
        status = _read_status(run_id, run_dir)
        phase = str(status.get("phase") or "")
        if phase not in {"publishing", "scraping"}:
            return False
        total = int(status.get("batches_total") or 0)
        if total <= 0:
            return False
        raw, err = await asyncio.to_thread(count_done_batches, run_dir)
        status["batches_done"] = raw + err
        status["scrapedo_failed_requests"] = err
        status["updated_at"] = utc_now_iso()
        if raw + err >= total:
            status["phase"] = "cleaning"
            dispatch = True
        _persist_status(run_id, run_dir, status)
        _last_flush[run_id] = time.monotonic()
    if dispatch:
        maybe_start_finish(run_id)
    return dispatch


def maybe_start_finish(run_id: str) -> None:
    """Dispatch run_ai_mode_finish for a run, at most one live task per run."""
    existing = _finish_tasks.get(run_id)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(asyncio.to_thread(svc.run_ai_mode_finish, run_id))
    _finish_tasks[run_id] = task

    def _cleanup(t: asyncio.Task, *, _run_id: str = run_id) -> None:
        if _finish_tasks.get(_run_id) is t:
            _finish_tasks.pop(_run_id, None)
        if not t.cancelled() and t.exception() is not None:
            _LOG.error("run_ai_mode_finish task crashed for %s", _run_id,
                       exc_info=t.exception())

    task.add_done_callback(_cleanup)


# --------------------------------------------------------------------------- #
# Reconciler — the always-on durability backstop (worker process: startup sweep
# + folded into the engine's periodic_batch_reconciler)
# --------------------------------------------------------------------------- #
_TERMINAL_STATUSES = {"completed", "completed_with_errors", "failed"}


def _parse_iso_epoch(ts: Any) -> Optional[float]:
    """ISO-8601 timestamp -> epoch seconds (None when missing/malformed)."""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(ts))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _newest_activity_epoch(run_dir: Path, status: dict) -> Optional[float]:
    """Latest sign of life: newest raw/error file mtime, else status updated_at."""
    newest = _parse_iso_epoch(status.get("updated_at"))
    raw_dir = run_dir / RAW_RESPONSES_DIRNAME
    try:
        with os.scandir(raw_dir) as it:
            for entry in it:
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if newest is None or mtime > newest:
                    newest = mtime
    except OSError:
        pass
    return newest


def _present_indices(run_dir: Path) -> set[int]:
    """Batch indices that already have a raw file OR an error marker."""
    raw_idx, err_idx = _scan_indices(run_dir)
    return raw_idx | err_idx


def _raw_parseable(run_dir: Path, request_index: int) -> bool:
    """True iff the batch's raw file exists AND parses (a crash mid-write can
    leave a truncated file; resume must retry those, not skip them)."""
    path = raw_file_path(run_dir, request_index)
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except (ValueError, OSError):
        return False


def _build_scrape_payload(
    run_id: str, request_index: int, batches_total: int,
    mode_key: str, company_name: str, group: list[Entity],
) -> dict[str, Any]:
    return {
        "type": "scrape",
        "run_id": run_id,
        "request_index": request_index,
        "batches_total": batches_total,
        "mode": mode_key,
        "company_name": company_name,
        "entities": [asdict(e) for e in group],
        "published_at": utc_now_iso(),
    }


async def _flip_phantom_run(run_id: str, run_dir: Path, actions: dict) -> None:
    """Honest-status fix: a hard-killed run can never flip itself to failed (the
    except-path needs a live process), so the reconciler does it — which makes
    the existing "Rerun failed" button appear and the resume endpoint accept."""
    async with get_run_lock(run_id):
        status = _read_status(run_id, run_dir)
        if str(status.get("status") or "") not in {"queued", "running"}:
            return
        status["status"] = "failed"
        status["error"] = (
            "Run interrupted (server crash or restart). Use 'Rerun failed' to "
            "resume — already-scraped and already-cleaned batches are reused, "
            "not re-billed."
        )
        status["updated_at"] = utc_now_iso()
        _persist_status(run_id, run_dir, status)
    await asyncio.to_thread(
        svc._supabase_update_run, status.get("run_db_id"),
        status="failed", error=status["error"], finished_at=status["updated_at"],
    )
    actions["phantoms_failed"] += 1
    _LOG.warning("reconciler flipped phantom run %s to failed", run_id)


async def _reconcile_scrape_run(
    run_id: str, run_dir: Path, actions: dict,
    *, stale_sec: int, max_requeue: int, now: float,
) -> None:
    """Republish lost/stale batches (attempt-capped, then terminalized)."""
    async with get_run_lock(run_id):
        status = _read_status(run_id, run_dir)
        if str(status.get("phase") or "") not in {"publishing", "scraping"}:
            return
        total = int(status.get("batches_total") or 0)
        if total <= 0:
            return
        present = await asyncio.to_thread(_present_indices, run_dir)
        missing = [idx for idx in range(1, total + 1) if idx not in present]
        if missing:
            newest = await asyncio.to_thread(_newest_activity_epoch, run_dir, status)
            if newest is not None and now - newest <= stale_sec:
                return  # still in-flight; leave it alone
            mode = get_mode(str(status.get("mode") or "ai_bulk"))
            attempts_map: dict[str, int] = dict(status.get("requeue_attempts") or {})
            to_republish = [i for i in missing
                            if int(attempts_map.get(str(i), 0) or 0) < max_requeue]
            to_terminalize = [i for i in missing if i not in set(to_republish)]
            for idx in to_terminalize:
                attempts = int(attempts_map.get(str(idx), 0) or 0)
                await asyncio.to_thread(
                    write_error_marker, run_dir, mode.key, idx,
                    f"terminalized by reconciler after {attempts} requeue(s) "
                    f"(message lost/stale > {stale_sec}s)",
                    note="reconciler",
                )
                actions["terminalized"] += 1
            if to_republish:
                try:
                    batch_size = int(status.get("batch_size") or 0) or mode.batch_size()
                except (TypeError, ValueError):
                    batch_size = mode.batch_size()
                groups = await asyncio.to_thread(_load_groups, run_dir, batch_size)
                company_name = str(status.get("company_name") or "")
                for idx in to_republish:
                    if idx > len(groups):
                        continue
                    payload = _build_scrape_payload(
                        run_id, idx, total, mode.key, company_name, groups[idx - 1]
                    )
                    try:
                        await broker.publish_scrape_job(payload)
                    except Exception:
                        _LOG.warning("reconciler republish failed run=%s idx=%s",
                                     run_id, idx, exc_info=True)
                        continue
                    attempts_map[str(idx)] = int(attempts_map.get(str(idx), 0) or 0) + 1
                    actions["republished"] += 1
            status["requeue_attempts"] = attempts_map
            status["updated_at"] = utc_now_iso()
            _persist_status(run_id, run_dir, status)
            _last_flush[run_id] = time.monotonic()
    # Outside the lock: recount + flip if everything is terminal now (covers a
    # missed winner-check and the just-terminalized markers above).
    if await _completion_check(run_id, run_dir):
        actions["finish_dispatched"] += 1


async def reconcile_ai_mode_runs() -> dict:
    """Sweep non-terminal AI Mode runs and self-heal (SerpWow-reconciler parity).

    1. broker runs stuck publishing/scraping with lost batches: republish each
       missing batch up to AI_MODE_BATCH_MAX_REQUEUE times (safe — workers skip
       existing raw files), then terminalize via error markers so the barrier
       ALWAYS resolves. Gated on a drained queue, like reconcile_stuck_gsearch_rows.
    2. broker runs in phase "cleaning" with no live finish task (worker died
       mid-cleanup): re-dispatch run_ai_mode_finish (resumable via cleaned/).
    3. legacy sync-engine runs stuck queued/running past AI_MODE_LEGACY_STALE_SEC:
       flip to failed so the UI's "Rerun failed" button appears (phantom fix).
    """
    scan_limit = max(1, _int_env("AI_MODE_RECONCILE_SCAN_LIMIT", 500))
    stale_sec = max(30, _int_env("AI_MODE_BATCH_STALE_TIMEOUT_SEC", 900))
    legacy_stale_sec = max(60, _int_env("AI_MODE_LEGACY_STALE_SEC", 3600))
    max_requeue = max(0, _int_env("AI_MODE_BATCH_MAX_REQUEUE", 1))
    now = time.time()
    actions = {"republished": 0, "terminalized": 0,
               "finish_dispatched": 0, "phantoms_failed": 0}

    for run_dir in run_store.list_run_dirs()[:scan_limit]:
        status_path = run_dir / "status.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(status, dict):
            continue
        run_status = str(status.get("status") or "")
        if not run_status or run_status in _TERMINAL_STATUSES:
            continue
        run_id = str(status.get("run_id") or run_dir.name)
        try:
            if str(status.get("engine") or "") != "broker":
                # Legacy in-process run: only the phantom-running flip applies.
                newest = _parse_iso_epoch(status.get("updated_at"))
                if newest is not None and now - newest > legacy_stale_sec:
                    await _flip_phantom_run(run_id, run_dir, actions)
                continue
            phase = str(status.get("phase") or "")
            if phase == "cleaning":
                existing = _finish_tasks.get(run_id)
                if existing is None or existing.done():
                    maybe_start_finish(run_id)
                    actions["finish_dispatched"] += 1
                continue
            if phase in {"publishing", "scraping"}:
                # Queue-dependent work: act only when the queue is drained, so a
                # slow-but-alive run is never double-published.
                depth = await broker.get_queue_depth()
                if depth is None or depth > 0:
                    continue
                await _reconcile_scrape_run(
                    run_id, run_dir, actions,
                    stale_sec=stale_sec, max_requeue=max_requeue, now=now,
                )
        except Exception:
            _LOG.warning("reconcile failed for run %s", run_id, exc_info=True)
    if any(actions.values()):
        _LOG.info("ai_mode reconcile actions: %s", actions)
    return actions


# --------------------------------------------------------------------------- #
# Consumer loops (mirror of the SerpWow worker's ack/poison policy)
# --------------------------------------------------------------------------- #
async def _should_requeue_infra_failure(
    payload: dict[str, Any] | None, message: Any,
) -> bool:
    """Restart-safe retry budget for infrastructure crashes.

    RabbitMQ's ``redelivered`` flag is unusable as a retry counter: graceful
    shutdown and stop-time nacks set it too, so after any deploy every
    prefetched in-flight message would lose its retry on the first transient
    failure. Track attempts durably in status.json (``delivery_failures``,
    capped at AI_MODE_BATCH_MAX_REQUEUE) instead; fall back to the redelivered
    flag only when the payload is undecodable/unresolvable.
    """
    fallback = not bool(getattr(message, "redelivered", False))
    try:
        if not payload or str(payload.get("type") or "scrape") == "check":
            # check messages are cheap and idempotent; the reconciler re-kicks
            # completion regardless, so one broker retry is plenty.
            return fallback
        request_index = int(payload.get("request_index") or 0)
        run_dir = _resolve_run_dir(payload)
        if run_dir is None or request_index <= 0:
            return fallback
        run_id = str(payload.get("run_id") or "")
        max_attempts = max(1, _int_env("AI_MODE_BATCH_MAX_REQUEUE", 1))
        async with get_run_lock(run_id):
            status = _read_status(run_id, run_dir)
            failures = dict(status.get("delivery_failures") or {})
            count = int(failures.get(str(request_index), 0) or 0)
            if count >= max_attempts:
                return False
            failures[str(request_index)] = count + 1
            status["delivery_failures"] = failures
            status["updated_at"] = utc_now_iso()
            _persist_status(run_id, run_dir, status)
        return True
    except Exception:
        return fallback


def _terminalize_failed_payload(payload: dict[str, Any] | None, exc: BaseException) -> None:
    """Poison message dropped after redelivery: leave a durable error marker so
    the run's barrier still resolves instead of wedging forever."""
    try:
        if not payload or str(payload.get("type") or "scrape") == "check":
            return
        request_index = int(payload.get("request_index") or 0)
        if request_index <= 0:
            return
        run_dir = _resolve_run_dir(payload)
        if run_dir is None:
            return
        write_error_marker(
            run_dir, str(payload.get("mode") or "ai_bulk"), request_index,
            f"dropped after redelivery failure: {exc}",
            note="worker terminalized a poison message",
        )
    except Exception:
        _LOG.warning("failed to terminalize poison ai_mode message", exc_info=True)


async def ai_mode_worker_loop(worker_id: int) -> None:
    job_timeout = max(30, _int_env("AI_MODE_JOB_TIMEOUT_SEC", 600))
    while _stop_event is not None and not _stop_event.is_set():
        queue = broker.ai_mode_queue
        if queue is None:
            await asyncio.sleep(1.0)
            continue
        try:
            async with queue.iterator() as queue_iter:
                async for message in queue_iter:
                    if _stop_event is None or _stop_event.is_set():
                        await message.nack(requeue=True)
                        break
                    payload: dict[str, Any] | None = None
                    try:
                        payload = json.loads(message.body.decode("utf-8"))
                        await asyncio.wait_for(
                            process_scrape_job(payload), timeout=job_timeout
                        )
                        # Ack ONLY after the raw/error file is durably on disk.
                        await message.ack()
                    except asyncio.CancelledError:
                        await message.nack(requeue=True)
                        raise
                    except Exception as exc:
                        if await _should_requeue_infra_failure(payload, message):
                            # Transient infra failure: retry via redelivery.
                            try:
                                await message.nack(requeue=True)
                            except Exception:
                                pass
                        else:
                            # Retry budget exhausted: terminalize + drop
                            # (poison guard — the barrier still resolves).
                            _terminalize_failed_payload(payload, exc)
                            try:
                                await message.reject(requeue=False)
                            except Exception:
                                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.warning("ai_mode worker %s consumer error; retrying", worker_id,
                         exc_info=True)
            await asyncio.sleep(1.0)


async def start_ai_mode_consumers(worker_count: Optional[int] = None) -> None:
    """Start AI-Mode consumer loops (call from the worker process only)."""
    global _stop_event, _consumer_tasks
    if _consumer_tasks:
        return
    if broker.ai_mode_queue is None:
        raise RuntimeError("AI Mode queue is not initialized")
    _stop_event = asyncio.Event()
    count = max(1, int(worker_count if worker_count is not None
                       else broker.worker_concurrency()))
    # scrape_batch_sync runs via asyncio.to_thread on the default executor, whose
    # stock cap (~min(32, cpus+4)) would silently throttle high AI-Mode
    # concurrency. Size it to cover both consumer fleets plus headroom.
    from concurrent.futures import ThreadPoolExecutor

    pool_size = count + max(1, _int_env("WORKER_CONCURRENCY", 4)) + 8
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="ai-mode-worker")
    )
    _consumer_tasks = [
        asyncio.create_task(ai_mode_worker_loop(i + 1)) for i in range(count)
    ]


async def stop_ai_mode_consumers() -> None:
    global _stop_event, _consumer_tasks
    if not _consumer_tasks:
        return
    if _stop_event is not None:
        _stop_event.set()
    done, pending = await asyncio.wait(_consumer_tasks, timeout=2.0)
    for task in pending:
        task.cancel()
    for task in _consumer_tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _consumer_tasks = []
    _stop_event = None
    # Let any in-flight finish tasks complete on their own; they are resumable
    # and the reconciler re-dispatches them after a restart.
