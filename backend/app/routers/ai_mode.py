# backend/app/routers/ai_mode.py
"""AI-mode endpoints (ai_bulk / ai_deep unified engine)."""
import asyncio
import json
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.models.entities import InvalidCSVError
from app.services.ai_mode.mode_config import MODES
from app.services.companies import get_company_service

router = APIRouter()
ai_mode_tasks: set[asyncio.Task] = set()


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
    company = await asyncio.to_thread(svc.get_company, company_id)
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
