# backend/app/services/serpwow/row_logging.py
"""Per-row trace logging helpers for SerpWow pipelines."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.services.common.env import get_bool_env


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_text(value: Any, limit: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:max(0, limit - 3)]}..."


def _pipeline_logs_enabled() -> bool:
    # Verbose-by-default for testing and triage.
    return get_bool_env("ENABLE_VERBOSE_ROW_LOGS", True)


def _log_row_stage(
    stage: str,
    message: str,
    *,
    upload_id: Optional[str] = None,
    row_index: Optional[int] = None,
    level: str = "INFO",
) -> None:
    if not _pipeline_logs_enabled():
        return
    upload_token = str(upload_id or "-")
    row_token = str(row_index if row_index is not None else "-")
    print(
        f"[{level}][row-trace][{_now_iso()}][stage:{stage}]"
        f"[upload:{upload_token}][row:{row_token}] {message}"
    )
