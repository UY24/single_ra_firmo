# backend/app/routers/ai_mode.py
"""AI-mode endpoints (ai_bulk / ai_deep unified engine)."""
import asyncio
import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.core.supabase_client import get_supabase_config_error
from app.models.entities import InvalidCSVError, parse_entities_csv
from app.services.ai_mode.mode_config import MODES
from app.services.companies import get_company_service

router = APIRouter()
ai_mode_tasks: set[asyncio.Task] = set()
# Runs with a resume currently being prepared: two concurrent resume requests
# could both pass the 409 status gate before either flips the status, then
# double-publish (and double-bill) every missing batch.
_resume_in_flight: set[str] = set()


def _broker_unavailable_detail() -> str:
    from app.services.ai_mode import broker as ai_broker

    detail = "AI Mode job queue unavailable (RabbitMQ not connected)"
    if ai_broker.last_error:
        detail = f"{detail}: {ai_broker.last_error}"
    return detail


def _supabase_not_configured_detail() -> str:
    detail = "Supabase not configured (set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)"
    error = get_supabase_config_error()
    return f"{detail}: {error}" if error else detail


def build_preview(raw: bytes) -> dict:
    """Parse a CSV and return what the engine would see (no run is created)."""
    parsed = parse_entities_csv(raw)
    return {"total_rows": len(parsed.entities),
            "columns_detected": parsed.columns_detected,
            "warnings": parsed.warnings,
            "positional": parsed.positional,
            "sample_rows": [asdict(e) for e in parsed.entities[:5]]}


@router.post("/uploads/preview")
async def preview_upload(file: UploadFile = File(...)) -> dict:
    try:
        return build_preview(await file.read())
    except InvalidCSVError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/uploads/ai-mode")
async def create_ai_mode_upload(
    file: UploadFile = File(...),
    mode: str = Form("ai_bulk"),
    company_id: str = Form(...),
) -> dict[str, Any]:
    from app.services.ai_mode import ai_mode_service
    if mode not in MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {sorted(MODES)}")
    from app.services.ai_mode import broker as ai_broker

    # Gate BEFORE prepare so a broker outage never leaves an orphan run dir
    # (same contract as SerpWow's 503 when RabbitMQ is down).
    if not ai_broker.is_ready():
        raise HTTPException(status_code=503, detail=_broker_unavailable_detail())
    svc = get_company_service()
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail=_supabase_not_configured_detail(),
        )
    try:
        company = await asyncio.to_thread(svc.get_company, company_id)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Supabase unreachable — check SUPABASE_URL / project status",
        ) from exc
    if company is None:
        raise HTTPException(
            status_code=400, detail="unknown company_id — create the company first"
        )
    raw = await file.read()
    try:
        info = ai_mode_service.prepare_ai_mode_run(
            raw,
            file.filename or "",
            mode_key=mode,
            company_name=company["name"],
            company_id=company_id,
        )
    except InvalidCSVError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        # Config errors (e.g. missing LLM API key) are client-visible 400s,
        # not 500s; prepare_ai_mode_run leaves no run dir behind in this case.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Best-effort Supabase run tracking: create_run never raises (returns None on
    # failure) and the run proceeds untracked if Supabase is unhappy.
    run_db_id = await asyncio.to_thread(
        svc.create_run,
        company_id=company_id,
        pipeline=mode,
        run_ref=info["run_id"],
        total_rows=info["total_rows"],
    )
    if run_db_id:
        info["run_db_id"] = run_db_id
        await asyncio.to_thread(ai_mode_service.set_run_db_id, info["run_id"], run_db_id)
    # Publishing 100k messages takes tens of seconds — return immediately and
    # publish in the background; the worker process consumes as they land.
    from app.services.ai_mode import worker as ai_worker

    task = asyncio.create_task(ai_worker.publish_run_batches(info["run_id"]))
    ai_mode_tasks.add(task)
    task.add_done_callback(ai_mode_tasks.discard)
    info["engine"] = "broker"
    return info


@router.post("/uploads/ai-mode/{run_id}/resume")
async def resume_ai_mode_upload(run_id: str) -> dict[str, Any]:
    """Re-run a failed/partial run IN PLACE (same run_id) — the only retry action.

    The UI labels this "Rerun failed". Broker engine: terminal error markers are
    cleared (those batches get retried) and ONLY batches without a parseable raw
    file are republished — no scrape.do re-spend on successes; Phase 2 reuses
    existing ``cleaned/`` batches, so only failed LLM work is redone. Updates the
    existing Supabase run row (no new row). Genuinely not-found rows are NOT
    retried here — use AI Mode Deep (``ai_deep``) for those. Also the migration
    path for legacy (pre-broker) failed runs: same file layout, same resume.
    """
    from app.services.ai_mode import ai_mode_service, run_store, s3_sync
    from app.services.ai_mode import broker as ai_broker
    from app.services.ai_mode import worker as ai_worker

    if not ai_broker.is_ready():
        raise HTTPException(status_code=503, detail=_broker_unavailable_detail())
    run_dir = await asyncio.to_thread(run_store.find_run_dir, run_id)
    if run_dir is None:
        # Hosted/ephemeral disk: the local run dir may be gone, but the run was
        # mirrored to S3 — pull it back so resume can reuse raw_responses/cleaned.
        run_dir = await asyncio.to_thread(s3_sync.rehydrate_run_from_s3, run_id)
    if run_dir is None:
        raise HTTPException(
            status_code=404,
            detail="AI mode run not found (no local run dir, and nothing in S3 to restore)",
        )
    if run_id in _resume_in_flight:
        raise HTTPException(status_code=409, detail="resume already in progress for this run")
    _resume_in_flight.add(run_id)
    try:
        status = await asyncio.to_thread(ai_mode_service.get_ai_mode_status, run_id)
        state = str(status.get("status") or "")
        if state not in {"failed", "completed_with_errors"}:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"run status is '{state}'; resume only applies to failed or "
                    "completed_with_errors runs"
                ),
            )
        # reset flips status to running/publishing, so once we leave this block
        # a late duplicate request also fails the status gate above.
        cleared = await asyncio.to_thread(ai_worker.reset_run_for_resume, run_id, run_dir)
    finally:
        _resume_in_flight.discard(run_id)
    task = asyncio.create_task(
        ai_worker.publish_run_batches(run_id, only_missing=True)
    )
    ai_mode_tasks.add(task)
    task.add_done_callback(ai_mode_tasks.discard)
    return {
        "run_id": run_id,
        "status": "running",
        "resumed": True,
        "cleared_error_markers": cleared,
    }


@router.get("/uploads/ai-mode")
async def list_ai_mode_uploads() -> dict[str, Any]:
    from app.services.ai_mode import ai_mode_service
    runs = await asyncio.to_thread(ai_mode_service.list_ai_mode_runs)
    return {"count": len(runs), "runs": runs}


@router.get("/uploads/ai-mode/{run_id}/status")
async def ai_mode_status(run_id: str) -> dict[str, Any]:
    from app.services.ai_mode import ai_mode_service
    try:
        return await asyncio.to_thread(ai_mode_service.get_ai_mode_status, run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="AI mode run not found") from exc


@router.get("/uploads/ai-mode/{run_id}/result")
async def ai_mode_result(
    run_id: str,
    file: str = Query("final_report.json"),
    download: bool = Query(False),
) -> Response:
    from app.services.ai_mode import ai_mode_service
    try:
        path = await asyncio.to_thread(ai_mode_service.get_ai_mode_result_path, run_id, file)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="AI mode run not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Requested file is not available yet") from exc
    body = await asyncio.to_thread(path.read_bytes)
    media_type = "application/json" if file.endswith(".json") else ("text/csv" if file.endswith(".csv") else "text/plain")
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{run_id}_{file}"'
    if file.endswith(".json"):
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            pass
        else:
            body = json.dumps(
                ai_mode_service.sanitize_for_response(parsed),
                indent=2,
                ensure_ascii=False,
            ).encode("utf-8")
    return Response(content=body, media_type=media_type, headers=headers)
