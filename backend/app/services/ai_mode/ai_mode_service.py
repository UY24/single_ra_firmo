"""AI Mode orchestrator — one engine, two configs (spec §5).

Drives the AI Mode pipeline (scrape.do Google AI Mode -> LLM cleanup) for both
``ai_bulk`` and ``ai_deep`` modes and writes results under
``ai_mode_results/<company_slug>/<run_id>/`` (spec §6).

This module is a pure-sync orchestration layer. The scrape phase is driven by
the RabbitMQ worker (``services/ai_mode/worker.py``) one batch at a time via
``scrape_batch_sync``; once every batch has a raw file or error marker, the
worker dispatches ``run_ai_mode_finish`` (Phases 2+3: LLM cleanup + streaming
assembly). Both are intended to run in a thread (``asyncio.to_thread``) and
never raise — failures land in the run's ``status.json`` (status="failed").

Public API (other modules depend on these names/signatures):
    prepare_ai_mode_run(raw_csv, filename, *, mode_key, company_name, company_id) -> dict
    scrape_batch_sync(run_dir, mode, settings, scrapedo_client, request_index, group) -> dict
    run_ai_mode_finish(run_id, ...) -> None
    list_ai_mode_runs() -> list[dict]
    get_ai_mode_status(run_id) -> dict
    get_ai_mode_result_path(run_id, file_name) -> Path
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, TypeVar

from app.core.config import LEGACY_AI_MODE_RESULT_DIR
from app.models.entities import Entity, InvalidCSVError, format_entities_for_prompt, parse_entities_csv
from app.models.results import EntityResult
from app.services.common import llm_batch
from app.services.ai_mode import gemini_batch, run_store
from app.services.ai_mode.cost import build_cost_summary, calculate_llm_cost_usd
from app.services.ai_mode.cleanup import (
    build_cleanup_messages,
    coerce_json_array,
    parse_cleanup_response,
    parse_json_array_from_text,
)
from app.services.ai_mode.llm_client import make_llm_client, parse_gemini_usage
from app.services.ai_mode.mode_config import get_mode
from app.services.ai_mode.models import TokenUsage, utc_now_iso
from app.services.ai_mode.run_reporting import (  # noqa: F401 (classify_one_result re-exported)
    StreamingRunReport,
    classify_one_result,
    write_outputs,
)
from app.services.ai_mode.scrapedo_client import ScrapeDoClient
from app.services.ai_mode.settings import LLMConfig, Settings
from app.services.serpwow.outcomes import (
    SRC_GEMINI,
    SRC_SCRAPEDO,
    categorize_http_error,
)

ALLOWED_RESULT_FILES = {
    "final_report.json",
    "found.csv",
    "notFound.csv",
    "run.log",
    "input.csv",
}

RAW_RESPONSES_DIRNAME = "raw_responses"
CLEANED_DIRNAME = "cleaned"

# In-memory write-through cache of run status dicts.
_RUNS: dict[str, dict] = {}
_AI_MODE_LOGGER = logging.getLogger("ai_mode")

T = TypeVar("T")



def chunked(items: list[T], size: int) -> Iterable[list[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# --------------------------------------------------------------------------- #
# Logging (ONE run.log per run: leveled, secret-redacted)
# --------------------------------------------------------------------------- #
def _log_level() -> int:
    level_name = os.getenv("AI_MODE_LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, level_name, logging.INFO)


def _ensure_ai_mode_logger() -> logging.Logger:
    if not _AI_MODE_LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        _AI_MODE_LOGGER.addHandler(handler)
    _AI_MODE_LOGGER.setLevel(_log_level())
    _AI_MODE_LOGGER.propagate = False
    return _AI_MODE_LOGGER


def sanitize_secret_text(value: str) -> str:
    """Redact Scrape.do token query parameters in user-facing output."""
    return re.sub(r"([?&]token=)[^&'\"\s]+", r"\1[REDACTED]", value)


def sanitize_for_response(value: Any) -> Any:
    """Recursively redact secrets before returning JSON through the UI API."""
    if isinstance(value, str):
        return sanitize_secret_text(value)
    if isinstance(value, list):
        return [sanitize_for_response(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_for_response(item) for key, item in value.items()}
    return value


def _run_log_path(run_dir: Path) -> Path:
    return run_dir / "run.log"


def _ai_log(run_id: str, run_dir: Path, message: str, level: int = logging.INFO) -> None:
    safe_message = sanitize_secret_text(message)
    _ensure_ai_mode_logger().log(level, "[run:%s] %s", run_id, safe_message)
    if level < _log_level():
        return
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        with _run_log_path(run_dir).open("a", encoding="utf-8") as handle:
            handle.write(f"{utc_now_iso()} {logging.getLevelName(level)} {safe_message}\n")
    except OSError:
        _ensure_ai_mode_logger().warning(
            "[run:%s] failed to write AI Mode run log", run_id
        )


# --------------------------------------------------------------------------- #
# Geo targeting helpers
# --------------------------------------------------------------------------- #
def _geo_params_for_group(
    settings: Settings,
) -> tuple[dict[str, str], dict[str, Any]]:
    gl = settings.scrapedo_gl or "us"
    google_domain = settings.scrapedo_google_domain or "google.com"
    params: dict[str, str] = {"gl": gl, "google_domain": google_domain}
    if settings.scrapedo_hl:
        params["hl"] = settings.scrapedo_hl
    return params, {"gl": gl, "google_domain": google_domain}


# --------------------------------------------------------------------------- #
# Env parsing helpers (tolerate missing / malformed values)
# --------------------------------------------------------------------------- #
def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except (ValueError, TypeError):
        return default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except (ValueError, TypeError):
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _str_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


# --------------------------------------------------------------------------- #
# Config builders
# --------------------------------------------------------------------------- #
def build_ai_mode_settings() -> Settings:
    """Build (and validate) scrape.do Settings from the process environment.

    Reads website_url_finder's own process env (config.py has already loaded
    .env); does NOT load any scrape.do .env file. The engine batches by
    ModeConfig.batch_size().
    """
    settings = Settings(
        scrapedo_token=_str_env("SCRAPEDO_TOKEN"),
        batch_size=10,
        scrapedo_timeout_seconds=_float_env("SCRAPEDO_TIMEOUT_SECONDS", 90.0),
        scrapedo_max_retries=_int_env("SCRAPEDO_MAX_RETRIES", 2),
        scrapedo_max_query_chars=_int_env("SCRAPEDO_MAX_QUERY_CHARS", 6000),
        scrapedo_device=_str_env("SCRAPEDO_DEVICE"),
        scrapedo_hl=_str_env("SCRAPEDO_HL"),
        scrapedo_gl=_str_env("SCRAPEDO_GL"),
        scrapedo_google_domain=_str_env("SCRAPEDO_GOOGLE_DOMAIN"),
        scrapedo_safe=_str_env("SCRAPEDO_SAFE"),
        scrapedo_include_html=_bool_env("SCRAPEDO_INCLUDE_HTML"),
    )
    settings.validate()
    return settings


def build_ai_mode_llm_config() -> LLMConfig:
    """Build (and validate) the LLM config from the process environment.

    Gemini only. Reads process env directly; does NOT load a scrape.do .env file.

    The batch and inline paths differ in ONE thing, the model: a Batch job resolves through
    ``llm_batch.batch_model()`` (GEMINI_BATCH_MODEL -> GEMINI_MODEL -> default) so a run can
    be batched on a different model than it is inlined on. ``ai_bulk`` and ``ai_deep``
    resolve identically — this function does not know which mode it is for, and does not
    need to.
    """
    batch_mode = llm_batch.batch_enabled("ai_bulk")
    config = LLMConfig(
        api_key=_str_env("GEMINI_API_KEY"),
        model=(llm_batch.batch_model() if batch_mode
               else _str_env("GEMINI_MODEL") or "gemini-2.5-flash-lite"),
        max_retries=_int_env("LLM_MAX_RETRIES", 2),
        timeout_seconds=_float_env("LLM_TIMEOUT_SECONDS", 120.0),
    )
    config.validate()
    return config


# --------------------------------------------------------------------------- #
# scrape.do payload -> raw text for the cleanup LLM
# --------------------------------------------------------------------------- #
_LIST_BLOCK_TYPES = {"ordered_list", "unordered_list", "list"}


def _payload_text(payload: Any) -> str:
    """Flatten a scrape.do AI-Mode payload's text_blocks into plain text."""
    text_blocks = payload.get("text_blocks") if isinstance(payload, dict) else None
    lines: list[str] = []
    for block in text_blocks or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in _LIST_BLOCK_TYPES:
            for index, item in enumerate(block.get("list", []), start=1):
                snippet = item.get("snippet", "") if isinstance(item, dict) else str(item)
                if snippet:
                    lines.append(f"  {index}. {snippet}")
        else:
            snippet = block.get("snippet", "")
            if snippet:
                lines.append(snippet)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Run dir resolution (new layout + read-only legacy fallback)
# --------------------------------------------------------------------------- #
def _find_run_dir(run_id: str) -> Path | None:
    """Resolve a run dir: new ai_mode_results layout first, then the legacy dir."""
    run_dir = run_store.find_run_dir(run_id)
    if run_dir is not None:
        return run_dir
    legacy = LEGACY_AI_MODE_RESULT_DIR / run_id
    if legacy.is_dir():
        return legacy
    return None


def _available_files(run_dir: Path) -> list[str]:
    return sorted(name for name in ALLOWED_RESULT_FILES if (run_dir / name).exists())


def _request_failed(record: dict[str, Any]) -> bool:
    return bool(record.get("error")) or str(record.get("status") or "").lower() in {
        "error",
        "failed",
    }


def _scrapedo_request_failed(record: dict[str, Any]) -> bool:
    return _request_failed(record) and not record.get("raw_json_file")


def _failed_request_count(records: list[dict]) -> int:
    return sum(1 for record in records if isinstance(record, dict) and _request_failed(record))


def _scrapedo_failed_request_count(records: list[dict]) -> int:
    return sum(1 for record in records if isinstance(record, dict) and _scrapedo_request_failed(record))


def classify_ai_mode_outcomes(results: list[EntityResult]) -> tuple[dict, dict]:
    """Bucket finalized entities into found / not_found / error (SerpWow parity).

    Per-entity rule (evaluated in this order, so every entity lands in exactly
    one bucket -> ``found + not_found + errored == len(results)``):
      * ``website_url`` present            -> ``found``
      * genuine error (see below)          -> ``errored`` (attributed to a source)
      * otherwise (looked, found nothing)  -> ``not_found``

    An entity is a genuine error when it carries ``error_source`` (tagged in
    Phase 3: scrape.do batch failure -> ``scrapedo``; whole-batch LLM failure ->
    ``gemini``) OR it carries a plain ``error`` with no source. The latter is the
    "missing from LLM response" case (the entity was submitted and scraped OK but
    the LLM omitted a verdict for it): a genuine ``gemini`` failure that today only
    inflates ``websites_not_found``. It is tagged in place here so it is counted as
    an error and never mislabeled ``not_found``. ``not_found`` therefore only ever
    holds entities with neither a URL nor any error.

    Returns ``(outcome_breakdown, error_breakdown)`` where
    ``outcome_breakdown = {found, not_found, errored}`` and
    ``error_breakdown = {"by_source": {...}, "by_category": {...}}``.
    Mutates errored EntityResults' ``error_source``/``error_category`` so
    ``final_report.json`` carries them.
    """
    outcome_breakdown = {"found": 0, "not_found": 0, "errored": 0}
    by_source: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for r in results:
        bucket = classify_one_result(r)
        outcome_breakdown[bucket] += 1
        if bucket == "errored":
            by_source[r.error_source] = by_source.get(r.error_source, 0) + 1
            if r.error_category:
                by_category[r.error_category] = by_category.get(r.error_category, 0) + 1
    error_breakdown = {"by_source": by_source, "by_category": by_category}
    return outcome_breakdown, error_breakdown


def _reconcile_status_from_report(run_dir: Path, status: dict) -> dict:
    """Reflect request-level failures from a LEGACY report.json in the UI status.

    New-layout runs write request records into final_report.json and set their
    failure counts directly at completion, so this is a no-op for them.
    """
    report_path = run_dir / "report.json"
    if not report_path.exists():
        return status
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return status

    requests = report.get("requests")
    if not isinstance(requests, list):
        return status

    failed_request_count = _failed_request_count(requests)
    status["scrapedo_request_count"] = len(requests)
    status["failed_request_count"] = failed_request_count
    status["scrapedo_failed_requests"] = _scrapedo_failed_request_count(requests)

    if failed_request_count and status.get("status") == "completed":
        status["status"] = "completed_with_errors"
        status["error"] = f"{failed_request_count} request(s) failed."
    return status


# --------------------------------------------------------------------------- #
# Status persistence + accessors
# --------------------------------------------------------------------------- #
def _persist_status(run_id: str, run_dir: Path, status: dict) -> None:
    """Write ``status.json`` for the run and update the in-memory cache.

    Atomic (tmp + os.replace): the file is read cross-process (API UI polls,
    worker, reconciler) and flushed every ~2s during scraping — a truncating
    write_text could be torn by a hard kill or read mid-write, wedging the run.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    status["available_files"] = _available_files(run_dir)
    path = run_dir / "status.json"
    tmp = run_dir / "status.json.tmp"
    tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    _RUNS[run_id] = status


def _read_status(run_id: str, run_dir: Path) -> dict:
    """Load status.json, falling back to the in-memory cache."""
    status_path = run_dir / "status.json"
    if status_path.exists():
        try:
            return json.loads(status_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    cached = _RUNS.get(run_id)
    if cached is not None:
        return dict(cached)
    return {"run_id": run_id}


def set_status_fields(run_id: str, **fields: Any) -> None:
    """Merge extra fields into a run's persisted status.json (no-op if unknown)."""
    run_dir = run_store.find_run_dir(run_id)
    if run_dir is None:
        return
    status = _read_status(run_id, run_dir)
    status.update(fields)
    _persist_status(run_id, run_dir, status)


def set_run_db_id(run_id: str, run_db_id: str | None) -> None:
    """Persist the Supabase ``runs`` row id into the run's status.json (Task 14)."""
    if not run_db_id:
        return
    set_status_fields(run_id, run_db_id=run_db_id)


def _supabase_update_run(run_db_id: str | None, **fields: Any) -> None:
    """Best-effort Supabase run update — bookkeeping must NEVER affect the run."""
    if not run_db_id:
        return
    try:
        from app.services.companies import get_company_service

        svc = get_company_service()
        if svc is not None:
            svc.update_run(run_db_id, **fields)
    except Exception:
        _ensure_ai_mode_logger().exception(
            "supabase: run update failed for run_db_id=%s (run unaffected)", run_db_id
        )


def _build_run_update(summary: dict, file_links: dict[str, str]) -> dict:
    """Map an AI-mode terminal summary onto the Supabase ``runs`` row fields."""
    websites_found = summary.get("websites_found") or 0
    websites_not_found = summary.get("websites_not_found") or 0
    failed_request_count = summary.get("failed_request_count") or 0
    # 3-way taxonomy: success = found, failed = genuine errors (NOT not_found).
    # Falls back to the found/errored derivation for legacy summaries missing the
    # breakdown (found -> websites_found; errored -> failed_request_count).
    outcome = summary.get("outcome_breakdown") or {}
    found = outcome.get("found", websites_found)
    errored = outcome.get("errored", failed_request_count)
    return {
        "status": summary.get("status"),
        "success_count": found,
        "failed_count": errored,
        "websites_found": websites_found,
        "websites_not_found": websites_not_found,
        "token_usage": summary.get("token_usage"),
        "cost": summary.get("cost"),
        "model": summary.get("model"),
        "is_batch": summary.get("is_batch"),
        "duration_seconds": summary.get("batch_duration_seconds"),
        "file_links": file_links,
        "finished_at": summary.get("completed_at"),
        "error": (
            f"{failed_request_count} request(s) failed. See final_report.json."
            if failed_request_count
            else None
        ),
    }


def get_ai_mode_status(run_id: str) -> dict:
    """Return the current status dict for a run (new layout or legacy, read-only).

    Raises KeyError if the run is unknown. Refreshes ``available_files``.
    """
    run_dir = _find_run_dir(run_id)
    if run_dir is None:
        raise KeyError(run_id)
    status = _read_status(run_id, run_dir)
    status = _reconcile_status_from_report(run_dir, status)
    status["available_files"] = _available_files(run_dir)
    _RUNS[run_id] = status
    return status


def list_ai_mode_runs() -> list[dict]:
    """Return all known runs (new layout + legacy dir), newest first."""
    run_dirs = list(run_store.list_run_dirs())
    if LEGACY_AI_MODE_RESULT_DIR.exists():
        run_dirs.extend(p for p in LEGACY_AI_MODE_RESULT_DIR.iterdir() if p.is_dir())

    runs: list[dict] = []
    for run_dir in run_dirs:
        status_path = run_dir / "status.json"
        if not status_path.exists():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        status = _reconcile_status_from_report(run_dir, status)
        status["available_files"] = _available_files(run_dir)
        runs.append(status)
    runs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return runs


def get_ai_mode_result_path(run_id: str, file_name: str) -> Path:
    """Resolve a result file path for a run.

    Raises KeyError for an unknown run, ValueError for a disallowed file name, and
    FileNotFoundError when the file does not exist.
    """
    run_dir = _find_run_dir(run_id)
    if run_dir is None:
        raise KeyError(run_id)
    if file_name not in ALLOWED_RESULT_FILES:
        raise ValueError(f"File not allowed: {file_name}")
    path = run_dir / file_name
    if not path.exists():
        raise FileNotFoundError(str(path))
    return path


# --------------------------------------------------------------------------- #
# prepare_ai_mode_run
# --------------------------------------------------------------------------- #
def prepare_ai_mode_run(
    raw_csv: bytes,
    filename: str,
    *,
    mode_key: str,
    company_name: str,
    company_id: str,
) -> dict:
    """Validate an uploaded CSV, register a queued run, and persist initial state.

    Parses the canonical CSV format (raising InvalidCSVError for bad files),
    stores the raw CSV as input.csv under the per-company run dir, and writes a
    ``queued`` status.json. Does NOT call any external API.
    """
    if not (filename or "").lower().endswith(".csv"):
        raise InvalidCSVError("Only .csv file is supported.")

    mode = get_mode(mode_key)
    parsed = parse_entities_csv(raw_csv)
    total_rows = len(parsed.entities)

    # Build the LLM config only to surface the model label (no API call).
    # Validated BEFORE any files are written so a misconfigured server (e.g.
    # missing API key -> ValueError) never leaves an orphan run dir behind.
    llm_config = build_ai_mode_llm_config()
    batch_size = mode.batch_size()

    run_id = uuid.uuid4().hex
    run_dir = run_store.run_dir_for(company_name, run_id)
    now = utc_now_iso()
    try:
        (run_dir / "input.csv").write_bytes(raw_csv)
        status = _initial_status(
            run_id, mode, parsed, company_id=company_id, company_name=company_name,
            batch_size=batch_size, llm_config=llm_config, now=now,
        )
        _persist_status(run_id, run_dir, status)
    except BaseException:
        # Don't leave a half-written run dir (no/partial status.json) behind.
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    _ai_log(
        run_id,
        run_dir,
        f"AI Mode run prepared filename={filename or '-'} mode={mode.key} "
        f"company={company_name} total_rows={total_rows} batch_size={batch_size} "
        f"llm_model={llm_config.model}",
    )

    return {
        "run_id": run_id,
        "total_rows": total_rows,
        "mode": mode.key,
        "mode_label": mode.label,
        "company_id": company_id,
        "company_name": company_name,
        "columns_detected": parsed.columns_detected,
        "warnings": parsed.warnings,
        "llm_model": llm_config.model,
        "batch_size": batch_size,
        "created_at": now,
        "status_url": f"/uploads/ai-mode/{run_id}/status",
        "result_url": f"/uploads/ai-mode/{run_id}/result",
    }


def _initial_status(
    run_id: str,
    mode,
    parsed,
    *,
    company_id: str,
    company_name: str,
    batch_size: int,
    llm_config,
    now: str,
) -> dict:
    return {
        "run_id": run_id,
        "status": "queued",
        "mode": mode.key,
        "mode_label": mode.label,
        "company_id": company_id,
        "company_name": company_name,
        "columns_detected": parsed.columns_detected,
        "warnings": parsed.warnings,
        "total_rows": len(parsed.entities),
        "batch_size": batch_size,
        "llm_model": llm_config.model,
        "batches_total": 0,
        "batches_done": 0,
        "entities_processed": 0,
        "entities_without_scrape_data": 0,
        "llm_errors": 0,
        "websites_found": 0,
        "websites_not_found": 0,
        "failed_request_count": 0,
        "scrapedo_request_count": 0,
        "scrapedo_failed_requests": 0,
        "scrapedo_seconds_total": 0.0,
        "llm_seconds_total": 0.0,
        "token_usage": asdict(TokenUsage()),
        "created_at": now,
        "updated_at": now,
        "error": None,
    }


# --------------------------------------------------------------------------- #
# One-batch scraper (idempotent; also the broker worker's unit of work)
# --------------------------------------------------------------------------- #
def scrape_batch_sync(
    run_dir: Path,
    mode,
    settings: Settings,
    scrapedo_client,
    request_index: int,
    group: list[Entity],
) -> dict:
    """Scrape ONE batch to ``raw_responses/request_NNNNNN.json`` (idempotent).

    Returns a small metadata record WITHOUT the scrape payload — callers re-read
    the raw file from disk when they need the text, so holding a full run's
    records stays O(batches), never O(payload bytes) (1M-row memory safety).
    An existing parseable raw file is reused without re-scraping (resume, and
    broker redelivery idempotency in the queue engine).
    """
    from app.services.ai_mode import s3_sync

    raw_dir = run_dir / RAW_RESPONSES_DIRNAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_name = f"request_{request_index:06d}.json"
    raw_path = raw_dir / raw_name
    rel_raw_path = f"{RAW_RESPONSES_DIRNAME}/{raw_name}"
    geo_params, geo_debug = _geo_params_for_group(settings)
    # Resume/redelivery: reuse an existing, parseable raw response.
    if raw_path.exists():
        try:
            json.loads(raw_path.read_text(encoding="utf-8"))
            return {
                "request_index": request_index, "ok": True, "error": None,
                "scrapedo_seconds": 0.0, "rel_raw_path": rel_raw_path,
                "geo_debug": geo_debug, "reused": True,
            }
        except (ValueError, OSError):
            pass
    query = mode.search_prompt().replace("{entities}", format_entities_for_prompt(group))
    t0 = time.perf_counter()
    try:
        payload = scrapedo_client.search_google_ai_mode(query, extra_params=geo_params)
        seconds = time.perf_counter() - t0
        # Atomic write (tmp + os.replace): a crash mid-write must never leave a
        # truncated raw file — existence checks treat the file as "scraped".
        tmp_path = raw_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp_path, raw_path)
        # Stream the scrape to S3 immediately (best-effort).
        s3_sync.mirror_file_to_s3(run_dir, mode.key, raw_path)
        return {
            "request_index": request_index, "ok": True, "error": None,
            "scrapedo_seconds": seconds, "rel_raw_path": rel_raw_path,
            "geo_debug": geo_debug, "reused": False,
        }
    except Exception as exc:  # scrape.do failure for this batch
        seconds = time.perf_counter() - t0
        return {
            "request_index": request_index, "ok": False,
            "error": sanitize_secret_text(str(exc)),
            "scrapedo_seconds": seconds, "rel_raw_path": None,
            "geo_debug": geo_debug, "reused": False,
        }


# --------------------------------------------------------------------------- #
# Shared failure path + finish engine (Phases 2+3)
# --------------------------------------------------------------------------- #
def _fail_run(
    run_id: str, run_dir: Path, status: dict, exc: BaseException, wall_t0: float
) -> None:
    """Terminal-failure bookkeeping for run_ai_mode_finish: status.json ->
    failed, Supabase update, S3 mirror of the partial run dir, Slack failure
    ping. Never raises."""
    status["status"] = "failed"
    status["error"] = sanitize_secret_text(str(exc))
    status["updated_at"] = utc_now_iso()
    _persist_status(run_id, run_dir, status)
    _supabase_update_run(
        status.get("run_db_id"),
        status="failed",
        error=status["error"],
        duration_seconds=round(time.perf_counter() - wall_t0, 3),
        finished_at=utc_now_iso(),
    )
    _ai_log(
        run_id,
        run_dir,
        f"AI Mode run crashed error={status['error']}",
        logging.ERROR,
    )
    # Mirror the failed run dir to S3 too, so raw_responses/run.log are available
    # for debugging (best-effort; never mask the original failure).
    try:
        from app.services.ai_mode.s3_sync import mirror_run_to_s3
        mode_key = str(status.get("mode") or "ai_bulk")
        mirrored = mirror_run_to_s3(run_dir, mode_key)
        if mirrored:
            _ai_log(
                run_id,
                run_dir,
                f"Mirrored {len(mirrored)} file(s) of the failed run to S3 "
                f"under {run_dir.parent.name}/{mode_key}/{run_id}",
            )
    except Exception:  # never let mirroring obscure the crash
        pass
    # Slack ping on failure (best-effort; never mask the original crash).
    try:
        from app.core import notify
        notify.notify_run_failed(
            pipeline=str(status.get("mode_label") or status.get("mode") or "AI Mode"),
            company=status.get("company_name"),
            run_ref=run_id,
            error=status["error"],
            total_rows=status.get("total_rows"),
            duration_seconds=round(time.perf_counter() - wall_t0, 3),
        )
    except Exception:
        logging.getLogger("ai_mode").warning("slack notify (failed) failed", exc_info=True)


def _rebuild_phase1_recs(run_dir: Path, groups: list[list[Entity]]) -> list[dict]:
    """Rebuild Phase-1 batch records from disk (broker worker / crash recovery).

    File presence is the durable truth: a parseable raw file is a scraped batch;
    an ``request_NNNNNN.error.json`` marker is a terminal scrape failure; neither
    means the batch was lost (reported as an error so the run still terminalizes).
    Per-batch scrape timings are not persisted for successes, so they read 0.0 —
    the run-level total comes from the status.json counter instead.
    """
    raw_dir = run_dir / RAW_RESPONSES_DIRNAME
    recs: list[dict] = []
    for idx, group in enumerate(groups, start=1):
        raw_name = f"request_{idx:06d}.json"
        rec = {
            "request_index": idx,
            "group": group,
            "group_names": [e.company_name for e in group],
            "scrapedo_seconds": 0.0,
            "geo_debug": {},
            "reused": True,
        }
        ok = False
        raw_path = raw_dir / raw_name
        if raw_path.exists():
            try:
                json.loads(raw_path.read_text(encoding="utf-8"))
                ok = True
            except (ValueError, OSError):
                ok = False
        if ok:
            rec.update(ok=True, error=None,
                       rel_raw_path=f"{RAW_RESPONSES_DIRNAME}/{raw_name}")
        else:
            error = "batch was never scraped (message lost)"
            seconds = 0.0
            err_path = raw_dir / f"request_{idx:06d}.error.json"
            if err_path.exists():
                try:
                    marker = json.loads(err_path.read_text(encoding="utf-8"))
                    error = str(marker.get("error") or error)
                    seconds = float(marker.get("seconds") or 0.0)
                except (ValueError, OSError, TypeError):
                    pass
            rec.update(ok=False, error=error, rel_raw_path=None,
                       scrapedo_seconds=seconds)
        recs.append(rec)
    return recs


def run_ai_mode_finish(
    run_id: str,
    *,
    resume: bool = False,
    phase1_recs: list[dict] | None = None,
    started_at: str | None = None,
    wall_t0: float | None = None,
    scrape_stats: dict | None = None,
) -> None:
    """Phases 2+3: LLM-clean every scraped batch, stream-assemble the outputs.

    Standalone and resumable — the broker worker dispatches this once the scrape
    barrier resolves (and the reconciler re-dispatches it after a crash); the
    sync engine calls it with its in-memory ``phase1_recs`` so exact per-batch
    scrape timings are preserved. When ``phase1_recs`` is None, Phase-1 state is
    rebuilt from disk (raw files + error markers). Never raises: failures land
    in status.json (status="failed") via _fail_run.
    """
    run_dir = run_store.find_run_dir(run_id)
    if run_dir is None:
        _ensure_ai_mode_logger().error("[run:%s] run dir not found; cannot finish", run_id)
        return
    status = _read_status(run_id, run_dir)
    started_at = started_at or str(status.get("started_at") or utc_now_iso())
    if wall_t0 is None:
        wall_t0 = time.perf_counter()
    run_log = _run_log_path(run_dir)
    _report_in_progress: StreamingRunReport | None = None

    try:
        mode = get_mode(str(status.get("mode") or "ai_bulk"))
        # Honor the prepare-time batch size persisted in status.json (the same
        # value the publisher grouped by) — recomputing from env could regroup
        # entities differently and misalign answers with the wrong companies.
        try:
            batch_size = int(status.get("batch_size") or 0) or mode.batch_size()
        except (TypeError, ValueError):
            batch_size = mode.batch_size()
        cfg = build_ai_mode_llm_config()
        llm = make_llm_client(cfg)

        input_csv = run_dir / "input.csv"
        # Keep the ParsedCSV, not just its entities: Phase 3 needs columns_detected /
        # positional to pass the input's own columns through to found/notFound.csv,
        # and re-parsing a 1M-row CSV to learn its header would be absurd.
        parsed_input = parse_entities_csv(input_csv.read_bytes())
        entities: list[Entity] = parsed_input.entities
        groups = list(chunked(entities, batch_size))
        raw_dir = run_dir / RAW_RESPONSES_DIRNAME
        raw_dir.mkdir(parents=True, exist_ok=True)
        cleaned_dir = run_dir / CLEANED_DIRNAME
        cleaned_dir.mkdir(parents=True, exist_ok=True)
        from app.services.ai_mode import s3_sync

        batch_mode = llm_batch.batch_enabled("ai_bulk")
        status["status"] = "running"
        status["model"] = cfg.model
        status["is_batch"] = batch_mode
        status["batches_total"] = len(groups)

        # Phase-1 records: exact (sync engine) or rebuilt from disk (worker).
        if phase1_recs is not None:
            ordered = phase1_recs
        else:
            ordered = _rebuild_phase1_recs(run_dir, groups)
            _ai_log(
                run_id, run_dir,
                f"Finish: rebuilt {len(ordered)} Phase-1 record(s) from disk "
                f"(ok={sum(1 for r in ordered if r['ok'])})",
            )
        ok_batches = [r for r in ordered if r["ok"]]

        stats = dict(scrape_stats or {})
        entities_without_scrape_data = stats.get("entities_without_scrape_data")
        if entities_without_scrape_data is None:
            entities_without_scrape_data = sum(
                len(r["group"]) for r in ordered if not r["ok"]
            )
        scrapedo_seconds_total = stats.get("scrapedo_seconds_total")
        if scrapedo_seconds_total is None:
            scrapedo_seconds_total = float(status.get("scrapedo_seconds_total") or 0.0)

        per_request_records: list[dict] = []
        usage_total = TokenUsage()
        llm_errors = 0
        llm_seconds_total = 0.0

        # ------------------------------------------------------------- #
        # PHASE 2 - clean every scraped batch (sync OR Gemini Batch)
        # ------------------------------------------------------------- #
        status["phase"] = "cleaning"
        status["updated_at"] = utc_now_iso()
        _persist_status(run_id, run_dir, status)

        llm_error_by_index: dict[int, str | None] = {}
        llm_seconds_by_index: dict[int, float] = {}
        # Rare-case fallback: a batch whose cleaned/ write failed keeps its text
        # here so Phase 3 doesn't turn an LLM success into an error. Bounded by
        # write FAILURES only — normal batches live on disk, not in RAM.
        cleaned_write_fallback: dict[str, dict] = {}

        def _error_results(
            rec: dict, message: str,
            *, source: str | None = None, category: str | None = None,
        ) -> list[EntityResult]:
            return [
                EntityResult(
                    company_name=entity.company_name,
                    country=entity.country,
                    sno=entity.sno,
                    company_local_name=entity.company_local_name,
                    error=message,
                    error_source=source,
                    error_category=category,
                )
                for entity in rec["group"]
            ]

        def _usage_metadata(usage: TokenUsage) -> dict:
            # Store usage in Gemini's native usageMetadata shape so a resumed batch
            # (sync OR Gemini-batch origin) reads back through parse_gemini_usage.
            return {
                "promptTokenCount": usage.prompt_tokens,
                "candidatesTokenCount": usage.completion_tokens,
                "totalTokenCount": usage.total_tokens,
            }

        def _write_cleaned(key: str, text: str, usage_md: dict | None) -> None:
            # Persist one successfully-cleaned batch so a resume can skip it.
            # Best-effort: a write failure must never fail the run (the text is
            # kept in cleaned_write_fallback so Phase 3 still assembles it).
            if not (text or "").strip():
                return
            record = {"key": key, "text": text, "usage": usage_md}
            path = cleaned_dir / f"{key}.json"
            try:
                path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            except OSError as exc:
                _ai_log(run_id, run_dir, f"failed to write cleaned/{key}.json: {exc}", logging.WARNING)
                cleaned_write_fallback[key] = record
                return
            s3_sync.mirror_file_to_s3(run_dir, mode.key, path)

        def _load_cleaned(key: str) -> dict | None:
            # Return a previously-cleaned {key,text,usage} record, or None.
            path = cleaned_dir / f"{key}.json"
            if path.exists():
                try:
                    obj = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    obj = None
                if isinstance(obj, dict) and str(obj.get("text") or "").strip():
                    return obj
            return cleaned_write_fallback.get(key)

        def _load_raw_payload(request_index: int) -> Any:
            # Re-read one batch's scrape payload from disk (never held in RAM).
            path = raw_dir / f"request_{request_index:06d}.json"
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return None

        def _messages_for(rec: dict) -> list[dict]:
            payload = _load_raw_payload(rec["request_index"])
            return build_cleanup_messages(_payload_text(payload), rec["group"])

        if batch_mode:
            if not _str_env("GEMINI_API_KEY"):
                raise RuntimeError("GEMINI_API_KEY not configured (required for LLM_BATCH)")
            shard_size = llm_batch.shard_size()
            max_inflight = llm_batch.max_inflight()
            poll_sec = llm_batch.poll_sec()
            # Same concept and same 48h default as the other three pipelines, so it
            # shares GEMINI_BATCH_TIMEOUT_SEC now rather than carrying its own key.
            timeout_sec = llm_batch.timeout_sec()
            clean_t0 = time.perf_counter()

            def _key_for(rec: dict) -> str:
                return f"batch-{rec['request_index']:06d}"

            def _index_from_key(key: str) -> int | None:
                try:
                    return int(str(key).rsplit("-", 1)[-1])
                except (ValueError, TypeError):
                    return None

            # Resume: batches already cleaned on a prior attempt are skipped; only
            # the rest get re-submitted to Gemini (Phase 1 already reused raw files,
            # so scrape.do is never re-billed). Harmless on a fresh run.
            pending_recs = [rec for rec in ok_batches if _load_cleaned(_key_for(rec)) is None]
            already_cleaned = len(ok_batches) - len(pending_recs)
            # Shards hold small metadata records only; each shard's request bodies
            # are built just-in-time at submit (payload text re-read from disk) so
            # a 100k-batch run never holds every request body in RAM.
            shards = [
                pending_recs[i : i + shard_size]
                for i in range(0, len(pending_recs), shard_size)
            ]
            _ai_log(
                run_id, run_dir,
                f"Phase 2 Gemini batch: {len(pending_recs)} requests in {len(shards)} shard(s) "
                f"shard_size={shard_size} max_inflight={max_inflight} model={cfg.model}"
                + (f" (reusing {already_cleaned} already-cleaned)" if already_cleaned else ""),
            )

            job_names: list[str] = [] if resume else list(status.get("gemini_batch_jobs") or [])
            next_shard = 0
            inflight: dict[str, int] = {}  # batch_name -> shard index
            deadline = time.monotonic() + timeout_sec
            while next_shard < len(shards) or inflight:
                while next_shard < len(shards) and len(inflight) < max_inflight:
                    si = next_shard
                    next_shard += 1
                    # Build this shard's request bodies just-in-time and drop them
                    # after submit (memory stays O(one shard), not O(all batches)).
                    shard_items = [
                        (_key_for(rec),
                         gemini_batch.messages_to_gemini_request(_messages_for(rec)))
                        for rec in shards[si]
                    ]
                    create_obj = gemini_batch.create_batch(
                        cfg.model, shard_items, display_name=f"ai-mode-{run_id}-shard-{si + 1}"
                    )
                    del shard_items
                    name = gemini_batch.batch_name_from_create(create_obj)
                    if not name:
                        raise RuntimeError(
                            f"Gemini batch create returned no name for shard {si + 1}: {create_obj}"
                        )
                    inflight[name] = si
                    job_names.append(name)
                    status["gemini_batch_jobs"] = job_names
                    status["updated_at"] = utc_now_iso()
                    _persist_status(run_id, run_dir, status)
                    _ai_log(
                        run_id, run_dir,
                        f"Submitted Gemini batch shard {si + 1}/{len(shards)} job={name}",
                    )
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Gemini batch timed out after {timeout_sec}s; jobs={job_names}"
                    )
                terminal: list[str] = []
                for name, si in list(inflight.items()):
                    try:
                        batch_obj = gemini_batch.get_batch(name)
                    except Exception as exc:  # tolerate transient poll errors
                        _ai_log(
                            run_id, run_dir,
                            f"poll error job={name}: {sanitize_secret_text(str(exc))}",
                            logging.WARNING,
                        )
                        continue
                    sname = gemini_batch.state_name(batch_obj)
                    done = bool(batch_obj.get("done"))
                    if gemini_batch.is_terminal(sname, done):
                        if gemini_batch.is_success(sname, done, batch_obj):
                            for c in gemini_batch.collect_results(batch_obj):
                                key = c.get("key")
                                if not key:
                                    continue
                                if c.get("text") and not c.get("error"):
                                    # Durable checkpoint; Phase 3 reads it back
                                    # from disk (nothing retained in RAM).
                                    _write_cleaned(key, c["text"], c.get("usage"))
                                else:
                                    cidx = _index_from_key(key)
                                    if cidx is not None:
                                        llm_error_by_index[cidx] = sanitize_secret_text(
                                            f"LLM error: {c.get('error') or 'empty batch output'}"
                                        )
                            _ai_log(
                                run_id, run_dir,
                                f"Gemini batch shard {si + 1} succeeded job={name} state={sname}",
                            )
                        else:
                            _ai_log(
                                run_id, run_dir,
                                f"Gemini batch shard {si + 1} failed job={name} state={sname}",
                                logging.ERROR,
                            )
                        terminal.append(name)
                for name in terminal:
                    inflight.pop(name, None)
                if next_shard < len(shards) or inflight:
                    time.sleep(poll_sec)
                    # Heartbeat: a Gemini batch can take hours — keep updated_at
                    # fresh so the reconciler's staleness checks (and operators)
                    # never mistake a healthy long wait for a dead run.
                    status["updated_at"] = utc_now_iso()
                    _persist_status(run_id, run_dir, status)

            # (Per-batch resolution happens in Phase 3, reading cleaned/ files.)
            llm_seconds_total = time.perf_counter() - clean_t0
        else:
            for rec in ok_batches:
                idx = rec["request_index"]
                key = f"batch-{idx:06d}"
                # Resume: a previously-cleaned batch is left for Phase 3 to read.
                if _load_cleaned(key) is not None:
                    llm_seconds_by_index[idx] = 0.0
                    continue
                messages = _messages_for(rec)
                t0 = time.perf_counter()
                try:
                    parsed, usage = llm.complete_json(messages)
                    secs = time.perf_counter() - t0
                    parsed_array = coerce_json_array(parsed)
                    if parsed_array is None:
                        # Tokens were consumed even though the reply is unusable;
                        # count them here (successes are counted in Phase 3 from
                        # the cleaned file's usage metadata).
                        usage_total = usage_total + usage
                        msg = "LLM error: response was not a JSON array"
                        llm_error_by_index[idx] = msg
                        llm_errors += len(rec["group"])
                    else:
                        _write_cleaned(key, json.dumps(parsed_array, ensure_ascii=False), _usage_metadata(usage))
                except Exception as exc:
                    secs = time.perf_counter() - t0
                    msg = sanitize_secret_text(f"LLM error: {exc}")
                    llm_error_by_index[idx] = msg
                    llm_errors += len(rec["group"])
                llm_seconds_by_index[idx] = secs
                llm_seconds_total += secs
                status["llm_errors"] = llm_errors
                status["updated_at"] = utc_now_iso()
                _persist_status(run_id, run_dir, status)

        # ------------------------------------------------------------- #
        # PHASE 3 - stream assembly (in order): read each batch's cleaned
        # file from disk, classify, and write it straight into the report.
        # Memory stays O(one batch) — no run-wide results list.
        # ------------------------------------------------------------- #
        report = _report_in_progress = StreamingRunReport(
            run_dir,
            # found/notFound carry the uploaded CSV's own columns ahead of ours. The
            # writer streams input.csv itself; it needs the header that resolved to the
            # company name so it can replay parse_entities_csv's skip-blank-name rule.
            # Headerless (positional) input has no header to pass through and a
            # different skip rule — fall back to the classic columns.
            None if parsed_input.positional
            else parsed_input.columns_detected.get("company_name"))
        # Recount authoritatively while streaming (the sync path counted
        # provisionally above for incremental UI persists).
        llm_errors = 0
        for rec in ordered:
            idx = rec["request_index"]
            if not rec["ok"]:
                batch_results = _error_results(
                    rec, f"scrape.do error: {rec['error']}",
                    source=SRC_SCRAPEDO,
                    category=categorize_http_error(None, rec["error"] or ""),
                )
                _ai_log(
                    run_id,
                    run_dir,
                    f"batch {idx} scrape failed -> {len(rec['group'])} entities not found "
                    f"({rec['error']})",
                    logging.WARNING,
                )
                record = {
                    "request_index": idx,
                    "entity_count": len(rec["group"]),
                    "entity_names": rec["group_names"],
                    "status": "error",
                    "error": rec["error"],
                    "scrapedo_seconds": round(rec["scrapedo_seconds"], 3),
                    "llm_seconds": 0.0,
                    "combined_seconds": round(rec["scrapedo_seconds"], 3),
                    "raw_json_file": rec["rel_raw_path"],
                    "scrapedo_params": rec["geo_debug"],
                }
                report.add_batch(record, batch_results)
                per_request_records.append(record)
                continue
            key = f"batch-{idx:06d}"
            rec_error = llm_error_by_index.get(idx)
            loaded = None if rec_error else _load_cleaned(key)
            batch_results = []
            if rec_error is None and loaded is None:
                rec_error = (
                    "missing from LLM batch output"
                    if batch_mode
                    else "LLM error: batch not cleaned"
                )
            if rec_error is None:
                parsed_array = parse_json_array_from_text(loaded["text"])
                if parsed_array is None:
                    rec_error = "LLM error: could not parse JSON array from batch output"
                else:
                    usage_total = usage_total + parse_gemini_usage(loaded.get("usage"))
                    batch_results = parse_cleanup_response(parsed_array, rec["group"])
            if rec_error is not None:
                rec_error = sanitize_secret_text(rec_error)
                batch_results = _error_results(
                    rec, rec_error, source=SRC_GEMINI,
                    category=categorize_http_error(None, rec_error))
                llm_errors += len(rec["group"])
            for r in batch_results:
                if r.website_url:
                    _ai_log(
                        run_id,
                        run_dir,
                        f"batch {idx} {r.company_name} ({r.country}) -> "
                        f"found {r.website_url} (confidence={r.confidence}%)",
                    )
                else:
                    _ai_log(
                        run_id,
                        run_dir,
                        f"batch {idx} {r.company_name} ({r.country}) -> not found"
                        + (f" ({r.error})" if r.error else ""),
                    )
            llm_secs = llm_seconds_by_index.get(idx, 0.0)
            record = {
                "request_index": idx,
                "entity_count": len(rec["group"]),
                "entity_names": rec["group_names"],
                "status": "error" if rec_error else "success",
                "error": rec_error,
                "scrapedo_seconds": round(rec["scrapedo_seconds"], 3),
                "llm_seconds": round(llm_secs, 3),
                "combined_seconds": round(rec["scrapedo_seconds"] + llm_secs, 3),
                "raw_json_file": rec["rel_raw_path"],
                "scrapedo_params": rec["geo_debug"],
            }
            report.add_batch(record, batch_results)
            per_request_records.append(record)
            del batch_results

        # ----------------------------------------------------------------- #
        # Outputs: found.csv / notFound.csv / ONE final_report.json
        # ----------------------------------------------------------------- #
        total_wall = time.perf_counter() - wall_t0
        completed_at = utc_now_iso()
        failed_request_count = _failed_request_count(per_request_records)
        # 3-way outcome taxonomy (SerpWow parity): found / not_found / error.
        # The streaming report classified (and tagged) every row as it was added,
        # so the breakdowns are read off its counters — no results list needed.
        websites_found = report.websites_found
        websites_not_found = report.websites_not_found
        entities_processed = websites_found + websites_not_found
        outcome_breakdown = dict(report.counts)
        error_breakdown = {
            "by_source": dict(report.by_source),
            "by_category": dict(report.by_category),
        }
        errored = outcome_breakdown["errored"]
        # completed_with_errors iff there are genuine errors (a run with only
        # not_found rows is a clean `completed`). `errored` is a superset of
        # `failed_request_count` (it also counts per-entity LLM omissions).
        run_status = "completed_with_errors" if errored else "completed"

        # Per-run cost (Task 15): LLM tokens priced via env rates + scrape.do
        # per-request credits from response headers (env-rate estimate fallback).
        llm_usd = calculate_llm_cost_usd(
            prompt_tokens=usage_total.prompt_tokens,
            completion_tokens=usage_total.completion_tokens,
            batch_mode=batch_mode,
        )
        cost = build_cost_summary(
            llm_usd=llm_usd,
            request_count=len(per_request_records),
        )

        summary = {
            "run_id": run_id,
            "status": run_status,
            "mode": mode.key,
            "mode_label": mode.label,
            "company_id": status.get("company_id"),
            "company_name": status.get("company_name"),
            "generated_at": completed_at,
            "llm": {"base_url": cfg.base_url, "model": cfg.model},
            "batch_size": batch_size,
            "total_input_entities": len(entities),
            "entities_processed": entities_processed,
            "entities_without_scrape_data": entities_without_scrape_data,
            "llm_errors": llm_errors,
            "websites_found": websites_found,
            "websites_not_found": websites_not_found,
            "outcome_breakdown": outcome_breakdown,
            "error_breakdown": error_breakdown,
            "scrapedo_request_count": len(per_request_records),
            "failed_request_count": failed_request_count,
            "scrapedo_failed_requests": _scrapedo_failed_request_count(per_request_records),
            "model": cfg.model,
            "is_batch": batch_mode,
            "token_usage": asdict(usage_total),
            "cost": cost,
            "scrapedo_seconds_total": round(scrapedo_seconds_total, 3),
            "llm_seconds_total": round(llm_seconds_total, 3),
            "batch_duration_seconds": round(total_wall, 3),
            "started_at": started_at,
            "completed_at": completed_at,
        }
        output_paths = report.close(summary)

        # Per-request summary lines into the single run.log.
        for record in per_request_records:
            line = (
                f"request {record['request_index']} status={record['status']} "
                f"scrapedo={record['scrapedo_seconds']}s "
                f"llm={record['llm_seconds']}s "
                f"entities={record['entity_count']}"
            )
            if record.get("error"):
                line += f" {record['error']}"
            _ai_log(run_id, run_dir, line,
                    logging.ERROR if record.get("error") else logging.INFO)
        _ai_log(
            run_id,
            run_dir,
            f"Wrote outputs final_report.json found.csv notFound.csv "
            f"requests={len(per_request_records)} failed_requests={failed_request_count}",
        )

        # Reflect final summary counts in the status.
        status["status"] = run_status
        status["entities_processed"] = entities_processed
        status["entities_without_scrape_data"] = entities_without_scrape_data
        status["llm_errors"] = llm_errors
        status["scrapedo_request_count"] = len(per_request_records)
        status["failed_request_count"] = failed_request_count
        status["scrapedo_failed_requests"] = _scrapedo_failed_request_count(per_request_records)
        status["websites_found"] = websites_found
        status["websites_not_found"] = websites_not_found
        status["outcome_breakdown"] = outcome_breakdown
        status["error_breakdown"] = error_breakdown
        status["scrapedo_seconds_total"] = round(scrapedo_seconds_total, 3)
        status["llm_seconds_total"] = round(llm_seconds_total, 3)
        status["batch_duration_seconds"] = round(total_wall, 3)
        status["token_usage"] = asdict(usage_total)
        status["cost"] = cost
        status["completed_at"] = completed_at
        status["updated_at"] = utc_now_iso()
        status["error"] = (
            f"{failed_request_count} request(s) failed. See final_report.json."
            if failed_request_count
            else None
        )
        _persist_status(run_id, run_dir, status)
        # Tracked file links point to S3 (matches SerpWow's s3:// links) when S3 is
        # configured; fall back to the local path otherwise. The run dir is mirrored
        # to S3 (write-through during the run + the end-of-run mirror below), so
        # these URIs resolve to real objects.
        local_paths = dict(output_paths)
        local_paths["input.csv"] = input_csv
        local_paths["run.log"] = run_log
        file_links = {
            name: (s3_sync.run_s3_uri(run_dir, mode.key, name) or str(path))
            for name, path in local_paths.items()
        }
        _supabase_update_run(
            status.get("run_db_id"), **_build_run_update(summary, file_links)
        )
        _ai_log(
            run_id,
            run_dir,
            f"AI Mode run finished status={status['status']} "
            f"duration={total_wall:.2f}s failed_request_count={failed_request_count} "
            f"entities_processed={status['entities_processed']} "
            f"entities_without_scrape_data={entities_without_scrape_data}",
        )

        # Mirror the completed run dir to S3 (best-effort; never fails the run) —
        # the backstop that also writes the aggregates (final_report/found/notFound).
        mirrored = s3_sync.mirror_run_to_s3(run_dir, mode.key)
        if mirrored:
            _ai_log(
                run_id,
                run_dir,
                f"Mirrored {len(mirrored)} file(s) to S3 "
                f"under {run_dir.parent.name}/{mode.key}/{run_id}",
            )

        # Slack ping on completion (best-effort; notify never raises, but guard anyway).
        try:
            from app.core import notify
            _tokens = status.get("token_usage") or {}
            _cost = status.get("cost") or {}
            notify.notify_run_complete(
                pipeline=str(status.get("mode_label") or mode.label),
                company=status.get("company_name"),
                run_ref=run_id,
                status=status["status"],
                found=outcome_breakdown["found"],
                not_found=outcome_breakdown["not_found"],
                errored=outcome_breakdown["errored"],
                error_sources=error_breakdown["by_source"] or None,
                total_rows=status.get("total_rows"),
                searches=_cost.get("scrapedo_searches"),
                search_label="Scrape.do searches",
                tokens=_tokens.get("total_tokens"),
                input_tokens=_tokens.get("prompt_tokens"),
                output_tokens=_tokens.get("completion_tokens"),
                cost_usd=_cost.get("total_usd"),
                duration_seconds=status.get("batch_duration_seconds"),
                llm_errors=status.get("llm_errors"),
            )
        except Exception:
            logging.getLogger("ai_mode").warning("slack notify (complete) failed", exc_info=True)
    except Exception as exc:  # never raise to caller
        # Discard any half-written CSVs so a crashed finish never leaves
        # truncated found/notFound files served as complete.
        if _report_in_progress is not None:
            _report_in_progress.abort()
        _fail_run(run_id, run_dir, status, exc, wall_t0)
