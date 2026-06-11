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


@router.post("")
def create_company(body: CompanyIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "company name is required")
    try:
        return _svc().create_company(name)
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise HTTPException(409, f"company '{name}' already exists")
        raise


@router.get("")
def list_companies():
    return {"companies": _svc().list_companies()}


@router.get("/stats")
def company_stats():
    return {"companies": _svc().company_stats()}


@router.get("/runs")
def list_runs(company_id: str | None = None, pipeline: str | None = None):
    return {"runs": _svc().list_runs(company_id=company_id, pipeline=pipeline)}
