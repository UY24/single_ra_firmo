# backend/app/routers/ai_mode.py
"""AI-mode endpoints (ai_bulk / ai_deep unified engine)."""
import asyncio
import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.models.entities import InvalidCSVError, parse_entities_csv
from app.services.ai_mode.mode_config import MODES
from app.services.companies import get_company_service

router = APIRouter()
ai_mode_tasks: set[asyncio.Task] = set()


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
    svc = get_company_service()
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Supabase not configured (set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)",
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
    task = asyncio.create_task(asyncio.to_thread(ai_mode_service.run_ai_mode_sync, info["run_id"]))
    ai_mode_tasks.add(task)
    task.add_done_callback(ai_mode_tasks.discard)
    return info


@router.post("/uploads/ai-mode/{run_id}/rerun")
async def rerun_ai_mode_upload(run_id: str) -> dict[str, Any]:
    """Re-run a failed/partial run: retry only failed/unscraped rows, carry successes.

    NOTE: legacy-layout runs (``ai_mode_result/<run_id>``) resolve to no
    new-layout run dir and get a 404 here — they predate the company-aware
    layout and cannot be re-run.
    """
    from app.services.ai_mode import ai_mode_service, rerun, run_store

    prev_run_dir = await asyncio.to_thread(run_store.find_run_dir, run_id)
    if prev_run_dir is None:
        raise HTTPException(status_code=404, detail="AI mode run not found")
    prev_status = await asyncio.to_thread(ai_mode_service.get_ai_mode_status, run_id)
    prev_state = str(prev_status.get("status") or "")
    if prev_state not in {"failed", "completed_with_errors"}:
        raise HTTPException(
            status_code=400,
            detail=(
                f"run status is '{prev_state}'; only failed or "
                "completed_with_errors runs can be re-run"
            ),
        )
    try:
        retry_csv, carryover = await asyncio.to_thread(rerun.split_for_rerun, prev_run_dir)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=400,
            detail="previous run is missing final_report.json/input.csv; cannot re-run",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    svc = get_company_service()
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Supabase not configured (set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)",
        )
    mode = str(prev_status.get("mode") or "ai_bulk")
    company_id = str(prev_status.get("company_id") or "")
    company_name = str(prev_status.get("company_name") or "")
    # Refresh the company name from Supabase when possible, but fall back to
    # the name recorded in the previous run's status: a Supabase blip must not
    # block a re-run (the run dir slug only needs a stable display name).
    try:
        company = await asyncio.to_thread(svc.get_company, company_id) if company_id else None
    except Exception:
        company = None
    if company:
        company_name = company["name"]

    try:
        info = ai_mode_service.prepare_ai_mode_run(
            retry_csv.encode("utf-8"),
            f"rerun_of_{run_id}.csv",
            mode_key=mode,
            company_name=company_name,
            company_id=company_id,
        )
    except (InvalidCSVError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    new_run_id = info["run_id"]
    if carryover:
        new_run_dir = await asyncio.to_thread(run_store.find_run_dir, new_run_id)
        if new_run_dir is not None:
            await asyncio.to_thread(rerun.write_carryover, new_run_dir, carryover)
    info["rerun_of_run_id"] = run_id
    info["carried_over"] = len(carryover)
    await asyncio.to_thread(
        ai_mode_service.set_status_fields,
        new_run_id,
        rerun_of_run_id=run_id,
        carried_over=len(carryover),
    )
    # Best-effort Supabase tracking, linked to the previous run's row when known.
    run_db_id = await asyncio.to_thread(
        svc.create_run,
        company_id=company_id,
        pipeline=mode,
        run_ref=new_run_id,
        total_rows=info["total_rows"],
        rerun_of=prev_status.get("run_db_id"),
    )
    if run_db_id:
        info["run_db_id"] = run_db_id
        await asyncio.to_thread(ai_mode_service.set_run_db_id, new_run_id, run_db_id)
    task = asyncio.create_task(asyncio.to_thread(ai_mode_service.run_ai_mode_sync, new_run_id))
    ai_mode_tasks.add(task)
    task.add_done_callback(ai_mode_tasks.discard)
    return info


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
