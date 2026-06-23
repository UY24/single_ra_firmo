"""Company + run-row lifecycle. Bookkeeping only: update failures NEVER raise (spec §4)."""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class CompanyService:
    def __init__(self, client, retry_sleep: float = 1.0):
        self.client = client
        self.retry_sleep = retry_sleep

    # --- companies ---------------------------------------------------------
    def create_company(self, name: str) -> dict:
        res = self.client.table("companies").insert({"name": name.strip()}).execute()
        return res.data[0]

    def list_companies(self) -> list[dict]:
        return self.client.table("companies").select("*").order("created_at").execute().data

    def get_company(self, company_id: str) -> dict | None:
        data = (self.client.table("companies").select("*")
                .eq("id", company_id).execute().data)
        return data[0] if data else None

    # --- runs --------------------------------------------------------------
    def create_run(self, *, company_id: str, pipeline: str, run_ref: str,
                   total_rows: int | None = None, rerun_of: str | None = None) -> str | None:
        try:
            row = {"company_id": company_id, "pipeline": pipeline, "run_ref": run_ref,
                   "status": "queued", "total_rows": total_rows, "rerun_of": rerun_of}
            res = self.client.table("runs").insert(row).execute()
            return res.data[0]["id"]
        except Exception:
            logger.exception("supabase: create_run failed (run continues without tracking)")
            return None

    def update_run(self, run_db_id: str | None, **fields: Any) -> bool:
        if not run_db_id:
            return False
        for attempt in range(3):
            try:
                (self.client.table("runs").update(fields)
                 .eq("id", run_db_id).execute())
                return True
            except Exception:
                logger.exception("supabase: update_run attempt %s failed", attempt + 1)
                time.sleep(self.retry_sleep)
        logger.error("supabase: giving up updating run %s — stats lost, run unaffected",
                     run_db_id)
        return False

    def list_runs(self, company_id: str | None = None, pipeline: str | None = None,
                  limit: int = 200) -> list[dict]:
        q = self.client.table("runs").select("*")
        if company_id:
            q = q.eq("company_id", company_id)
        if pipeline:
            q = q.eq("pipeline", pipeline)
        return q.order("created_at", desc=True).limit(limit).execute().data

    def company_stats(self) -> list[dict]:
        """Aggregate per company in Python (internal tool, low volume)."""
        companies = self.list_companies()
        runs = self.client.table("runs").select(
            "company_id,status,total_rows,success_count,failed_count,"
            "websites_found,websites_not_found,cost,token_usage").execute().data
        by_company: dict[str, list[dict]] = {}
        for r in runs:
            by_company.setdefault(r["company_id"], []).append(r)
        out = []
        for c in companies:
            rs = by_company.get(c["id"], [])
            def s(key):
                return sum((r.get(key) or 0) for r in rs)
            cost = sum(((r.get("cost") or {}).get("total_usd") or 0) for r in rs)
            tokens = sum(((r.get("token_usage") or {}).get("total_tokens") or 0) for r in rs)
            input_tokens = sum(((r.get("token_usage") or {}).get("prompt_tokens") or 0) for r in rs)
            output_tokens = sum(((r.get("token_usage") or {}).get("completion_tokens") or 0) for r in rs)
            searches = sum(((r.get("cost") or {}).get("scrapedo_searches") or 0) for r in rs)
            out.append({**c, "runs": len(rs), "total_rows": s("total_rows"),
                        "success_count": s("success_count"), "failed_count": s("failed_count"),
                        "websites_found": s("websites_found"),
                        "websites_not_found": s("websites_not_found"),
                        "total_searches": searches,
                        "total_cost_usd": round(cost, 4), "total_tokens": tokens,
                        "total_input_tokens": input_tokens,
                        "total_output_tokens": output_tokens})
        return out


def get_company_service() -> CompanyService | None:
    from app.core.supabase_client import get_supabase
    client = get_supabase()
    return CompanyService(client) if client else None
