# backend/app/services/ai_mode/run_store.py
"""Run directories under ai_mode_results/<company_slug>/<run_id>/ (spec §6)."""
from __future__ import annotations

from pathlib import Path

from app.core.config import AI_MODE_RESULTS_DIR
from app.services.common.text import slugify_company  # re-exported for callers/tests


def run_dir_for(company_name: str, run_id: str) -> Path:
    d = AI_MODE_RESULTS_DIR / slugify_company(company_name) / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "raw_responses").mkdir(exist_ok=True)
    return d


def find_run_dir(run_id: str) -> Path | None:
    if not AI_MODE_RESULTS_DIR.exists():
        return None
    hits = list(AI_MODE_RESULTS_DIR.glob(f"*/{run_id}"))
    return hits[0] if hits else None


def list_run_dirs() -> list[Path]:
    if not AI_MODE_RESULTS_DIR.exists():
        return []
    return sorted((p for p in AI_MODE_RESULTS_DIR.glob("*/*") if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
