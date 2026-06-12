# backend/app/routers/companies.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.companies import get_company_service

router = APIRouter(prefix="/companies")


class CompanyIn(BaseModel):
    name: str


def _svc():
    svc = get_company_service()
    if svc is None:
        raise HTTPException(503, "Supabase not configured (set SUPABASE_URL + "
                                 "SUPABASE_SERVICE_ROLE_KEY)")
    return svc


def _unreachable(exc: Exception) -> HTTPException:
    """Unexpected service error (network down, bad DNS, …) → 503, not a raw 500."""
    return HTTPException(503, f"Supabase unreachable: {exc}")


@router.post("")
def create_company(body: CompanyIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "company name is required")
    svc = _svc()
    try:
        return svc.create_company(name)
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise HTTPException(409, f"company '{name}' already exists")
        raise _unreachable(exc)


@router.get("")
def list_companies():
    svc = _svc()
    try:
        return {"companies": svc.list_companies()}
    except Exception as exc:
        raise _unreachable(exc)


@router.get("/stats")
def company_stats():
    svc = _svc()
    try:
        return {"companies": svc.company_stats()}
    except Exception as exc:
        raise _unreachable(exc)


@router.get("/runs")
def list_runs(company_id: str | None = None, pipeline: str | None = None):
    svc = _svc()
    try:
        return {"runs": svc.list_runs(company_id=company_id, pipeline=pipeline)}
    except Exception as exc:
        raise _unreachable(exc)
