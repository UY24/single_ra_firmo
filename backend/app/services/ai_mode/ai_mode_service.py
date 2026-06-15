"""AI Mode orchestrator — one engine, two configs (spec §5).

Drives the AI Mode pipeline (scrape.do Google AI Mode -> LLM cleanup) for both
``ai_bulk`` and ``ai_deep`` modes and writes results under
``ai_mode_results/<company_slug>/<run_id>/`` (spec §6).

This module is a pure-sync orchestration layer. ``run_ai_mode_sync`` is intended
to be invoked from a thread (e.g. ``asyncio.to_thread``); it never raises to the
caller, instead reflecting any failure in the run's ``status.json``.

Public API (other modules depend on these names/signatures):
    prepare_ai_mode_run(raw_csv, filename, *, mode_key, company_name, company_id) -> dict
    run_ai_mode_sync(run_id) -> None
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, TypeVar

from app.core.config import LEGACY_AI_MODE_RESULT_DIR
from app.models.entities import Entity, InvalidCSVError, format_entities_for_prompt, parse_entities_csv
from app.models.results import EntityResult, Flag
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
from app.services.ai_mode.run_reporting import write_outputs
from app.services.ai_mode.scrapedo_client import ScrapeDoClient
from app.services.ai_mode.settings import DEFAULT_LLM_BASE_URLS, LLMConfig, Settings

ALLOWED_RESULT_FILES = {
    "final_report.json",
    "found.csv",
    "notFound.csv",
    "run.log",
    "input.csv",
}

RAW_RESPONSES_DIRNAME = "raw_responses"

# In-memory write-through cache of run status dicts.
_RUNS: dict[str, dict] = {}
_AI_MODE_LOGGER = logging.getLogger("ai_mode")

T = TypeVar("T")

COUNTRY_GL_ALIASES: dict[str, str] = {
    "united states": "us",
    "usa": "us",
    "u.s.a": "us",
    "u.s.": "us",
    "us": "us",
    "united kingdom": "gb",
    "uk": "gb",
    "great britain": "gb",
    "england": "gb",
    "gb": "gb",
    "india": "in",
    "bangladesh": "bd",
    "canada": "ca",
    "australia": "au",
    "germany": "de",
    "france": "fr",
    "italy": "it",
    "spain": "es",
    "netherlands": "nl",
    "sweden": "se",
    "norway": "no",
    "denmark": "dk",
    "finland": "fi",
    "japan": "jp",
    "south korea": "kr",
    "korea": "kr",
    "china": "cn",
    "singapore": "sg",
    "united arab emirates": "ae",
    "uae": "ae",
    "saudi arabia": "sa",
    "qatar": "qa",
    "kuwait": "kw",
    "oman": "om",
    "bahrain": "bh",
    "ireland": "ie",
    "poland": "pl",
    "switzerland": "ch",
    "austria": "at",
    "belgium": "be",
    "portugal": "pt",
    "mexico": "mx",
    "brazil": "br",
    "argentina": "ar",
    "south africa": "za",
    "new zealand": "nz",
    "turkiye": "tr",
    "turkey": "tr",
    "hungary": "hu",
    "nigeria": "ng",
    "colombia": "co",
    "estonia": "ee",
    "bulgaria": "bg",
    "latvia": "lv",
    "czech republic": "cz",
    "czechia": "cz",
    "thailand": "th",
    "serbia": "rs",
    "bosnia and herzegovina": "ba",
    "ecuador": "ec",
    "albania": "al",
    "egypt": "eg",
    "uruguay": "uy",
    "papua new guinea": "pg",
}

GOOGLE_DOMAIN_BY_GL: dict[str, str] = {
    "us": "google.com",
    "gb": "google.co.uk",
    "kr": "google.co.kr",
    "br": "google.com.br",
    "tr": "google.com.tr",
    "mx": "google.com.mx",
    "ar": "google.com.ar",
    "bd": "google.com.bd",
    "co": "google.com.co",
    "uy": "google.com.uy",
    "pg": "google.com.pg",
}


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
def _country_to_gl(country: str | None, fallback: str = "us") -> str:
    value = str(country or "").strip().lower()
    fallback_value = str(fallback or "us").strip().lower() or "us"
    if not value:
        return fallback_value
    if value in COUNTRY_GL_ALIASES:
        return COUNTRY_GL_ALIASES[value]
    compact = re.sub(r"[^a-z]", "", value)
    if compact in COUNTRY_GL_ALIASES:
        return COUNTRY_GL_ALIASES[compact]
    if len(compact) == 2:
        return compact
    return fallback_value


def _google_domain_for_gl(gl: str, fallback: str = "google.com") -> str:
    clean_gl = str(gl or "").strip().lower()
    if clean_gl in GOOGLE_DOMAIN_BY_GL:
        return GOOGLE_DOMAIN_BY_GL[clean_gl]
    return f"google.{clean_gl}" if len(clean_gl) == 2 else (fallback or "google.com")


def _entity_country(entity: Any) -> str:
    return str(getattr(entity, "country_code", "") or getattr(entity, "country", "") or "").strip()


def _location_from_entity(entity: Any, country: str) -> str:
    address = str(getattr(entity, "address", "") or "").replace("\n", ", ").strip(" ,")
    if not address:
        return country if len(country.strip()) > 2 else ""
    parts = [part.strip() for part in address.split(",") if part.strip()]
    location_parts = [part for part in parts if not re.fullmatch(r"[A-Za-z]{0,3}[- ]?\d{3,8}", part)]
    if len(location_parts) >= 3:
        return ",".join(location_parts[-3:-1] + [country])
    if len(location_parts) >= 2:
        return ",".join(location_parts[-2:] + [country])
    return ",".join([location_parts[0] if location_parts else address, country])


def _geo_params_for_group(
    group: list[Any],
    settings: Settings,
) -> tuple[dict[str, str], dict[str, Any]]:
    countries = [_entity_country(entity) for entity in group if _entity_country(entity)]
    unique_countries = []
    for country in countries:
        if country not in unique_countries:
            unique_countries.append(country)

    selected_country = unique_countries[0] if unique_countries else ""
    fallback_gl = settings.scrapedo_gl or "us"
    gl = _country_to_gl(selected_country, fallback=fallback_gl)
    google_domain = _google_domain_for_gl(gl, fallback=settings.scrapedo_google_domain or "google.com")

    params = {
        "gl": gl,
        "google_domain": google_domain,
    }
    if settings.scrapedo_hl:
        params["hl"] = settings.scrapedo_hl
    location = _location_from_entity(group[0], selected_country) if selected_country and group else ""
    if location:
        params["location"] = location

    return params, {
        "selected_country": selected_country,
        "countries": unique_countries,
        "mixed_countries": len(unique_countries) > 1,
        "gl": gl,
        "google_domain": google_domain,
        "location": location,
    }


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
    .env); does NOT load any scrape.do .env file. ``batch_size`` here is the
    legacy env value only; the engine batches by ModeConfig.batch_size().
    """
    settings = Settings(
        scrapedo_token=_str_env("SCRAPEDO_TOKEN"),
        batch_size=_int_env("SCRAPEDO_BATCH_SIZE", 10),
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

    Defaults to the Gemini provider. Reads process env directly; does NOT load a
    scrape.do .env file.
    """
    # Batch cleanup is Gemini-only, so AI_MODE_LLM_BATCH forces the Gemini provider
    # regardless of AI_MODE_LLM_PROVIDER (which governs only the normal/sync path).
    batch_mode = _bool_env("AI_MODE_LLM_BATCH", False)
    if batch_mode:
        provider = "gemini"
    else:
        provider = (_str_env("AI_MODE_LLM_PROVIDER", "gemini").lower()) or "gemini"
    if provider == "openai":
        api_key = _str_env("OPENAI_API_KEY")
        model = _str_env("OPENAI_MODEL") or "gpt-4o-mini"
        base_url = _str_env("OPENAI_BASE_URL") or DEFAULT_LLM_BASE_URLS["openai"]
    else:
        provider = "gemini"
        api_key = _str_env("GEMINI_API_KEY")
        if batch_mode:
            model = _str_env("GEMINI_BATCH_MODEL") or _str_env("GEMINI_MODEL") or "gemini-2.5-flash-lite"
        else:
            model = _str_env("GEMINI_MODEL") or "gemini-2.5-flash-lite"
        base_url = DEFAULT_LLM_BASE_URLS["gemini"]

    config = LLMConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        provider=provider,
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
    """Write ``status.json`` for the run and update the in-memory cache."""
    run_dir.mkdir(parents=True, exist_ok=True)
    status["available_files"] = _available_files(run_dir)
    (run_dir / "status.json").write_text(
        json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
    )
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
    llm_errors = summary.get("llm_errors") or 0
    failed_request_count = summary.get("failed_request_count") or 0
    return {
        "status": summary.get("status"),
        "success_count": websites_found,
        "failed_count": websites_not_found + llm_errors,
        "websites_found": websites_found,
        "websites_not_found": websites_not_found,
        "token_usage": summary.get("token_usage"),
        "cost": summary.get("cost"),
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

    # Build the LLM config only to surface provider/model labels (no API call).
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
        f"llm_provider={llm_config.provider} llm_model={llm_config.model}",
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
        "llm_provider": llm_config.provider,
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
        "llm_provider": llm_config.provider,
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
# run_ai_mode_sync (orchestrator)
# --------------------------------------------------------------------------- #
def run_ai_mode_sync(run_id: str) -> None:
    """Execute the full scrape.do -> LLM pipeline for a prepared run.

    Synchronous; intended to be called via ``asyncio.to_thread``. Never raises to
    the caller: any unexpected error is captured in status.json (status="failed").
    Persists status after every batch so a UI can poll progress.
    """
    run_dir = run_store.find_run_dir(run_id)
    if run_dir is None:
        _ensure_ai_mode_logger().error("[run:%s] run dir not found; cannot run", run_id)
        return
    status = _read_status(run_id, run_dir)
    started_at = utc_now_iso()
    wall_t0 = time.perf_counter()
    run_log = _run_log_path(run_dir)
    if run_log.exists():
        try:
            run_log.unlink()
        except OSError:
            pass
    _ai_log(run_id, run_dir, "AI Mode run starting")

    status["status"] = "running"
    status["started_at"] = started_at
    status["updated_at"] = utc_now_iso()
    status["error"] = None
    _persist_status(run_id, run_dir, status)
    _supabase_update_run(status.get("run_db_id"), status="running", started_at=started_at)

    try:
        mode = get_mode(str(status.get("mode") or "ai_bulk"))
        batch_size = mode.batch_size()
        search_prompt = mode.search_prompt()

        settings = build_ai_mode_settings()
        cfg = build_ai_mode_llm_config()
        _ai_log(
            run_id,
            run_dir,
            "Settings loaded "
            f"mode={mode.key} batch_size={batch_size} max_query_chars={settings.scrapedo_max_query_chars} "
            f"timeout={settings.scrapedo_timeout_seconds}s retries={settings.scrapedo_max_retries} "
            f"device={settings.scrapedo_device or '-'} hl={settings.scrapedo_hl or '-'} "
            f"gl={settings.scrapedo_gl or '-'} google_domain={settings.scrapedo_google_domain or '-'} "
            f"include_html={settings.scrapedo_include_html} llm_provider={cfg.provider} llm_model={cfg.model}",
        )
        llm = make_llm_client(cfg)
        scrapedo_client = ScrapeDoClient(
            token=settings.scrapedo_token,
            timeout_seconds=settings.scrapedo_timeout_seconds,
            max_retries=settings.scrapedo_max_retries,
            device=settings.scrapedo_device,
            hl=settings.scrapedo_hl,
            gl=settings.scrapedo_gl,
            google_domain=settings.scrapedo_google_domain,
            safe=settings.scrapedo_safe,
            include_html=settings.scrapedo_include_html,
            log=lambda message: _ai_log(run_id, run_dir, message, logging.DEBUG),
        )

        input_csv = run_dir / "input.csv"
        entities: list[Entity] = parse_entities_csv(input_csv.read_bytes()).entities
        _ai_log(
            run_id,
            run_dir,
            f"Loaded entities mode={mode.key} total_entities={len(entities)} input_csv={input_csv}",
        )

        groups = list(chunked(entities, batch_size))
        _ai_log(
            run_id,
            run_dir,
            f"Created {len(groups)} Scrape.do batch(es) from batch_size={batch_size}",
        )
        status["batches_total"] = len(groups)
        status["updated_at"] = utc_now_iso()
        _persist_status(run_id, run_dir, status)

        raw_dir = run_dir / RAW_RESPONSES_DIRNAME
        raw_dir.mkdir(parents=True, exist_ok=True)

        results: list[EntityResult] = []
        per_request_records: list[dict] = []
        usage_total = TokenUsage()
        entities_without_scrape_data = 0
        llm_errors = 0
        scrapedo_seconds_total = 0.0
        llm_seconds_total = 0.0

        batch_mode = _bool_env("AI_MODE_LLM_BATCH", False)
        concurrency = max(1, _int_env("SCRAPEDO_CONCURRENCY", 5))

        # ------------------------------------------------------------- #
        # PHASE 1 - scrape every batch (parallel, bounded by concurrency)
        # ------------------------------------------------------------- #
        status["phase"] = "scraping"
        status["updated_at"] = utc_now_iso()
        _persist_status(run_id, run_dir, status)

        def _scrape_one(request_index: int, group: list[Entity]) -> dict:
            group_names = [e.company_name for e in group]
            query = search_prompt.replace("{entities}", format_entities_for_prompt(group))
            geo_params, geo_debug = _geo_params_for_group(group, settings)
            raw_name = f"request_{request_index:03d}.json"
            raw_path = raw_dir / raw_name
            rel_raw_path = f"{RAW_RESPONSES_DIRNAME}/{raw_name}"
            # Resume: reuse an existing, parseable raw response instead of re-scraping.
            if raw_path.exists():
                try:
                    payload = json.loads(raw_path.read_text(encoding="utf-8"))
                    return {
                        "request_index": request_index, "group": group, "group_names": group_names,
                        "payload": payload, "error": None, "scrapedo_seconds": 0.0,
                        "rel_raw_path": rel_raw_path, "geo_debug": geo_debug,
                    }
                except (ValueError, OSError):
                    pass
            t0 = time.perf_counter()
            try:
                payload = scrapedo_client.search_google_ai_mode(
                    query, extra_params=geo_params
                )
                seconds = time.perf_counter() - t0
                raw_path.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                return {
                    "request_index": request_index, "group": group, "group_names": group_names,
                    "payload": payload, "error": None, "scrapedo_seconds": seconds,
                    "rel_raw_path": rel_raw_path, "geo_debug": geo_debug,
                }
            except Exception as exc:  # scrape.do failure for this batch
                seconds = time.perf_counter() - t0
                return {
                    "request_index": request_index, "group": group, "group_names": group_names,
                    "payload": None, "error": sanitize_secret_text(str(exc)),
                    "scrapedo_seconds": seconds, "rel_raw_path": None, "geo_debug": geo_debug,
                }

        scraped: dict[int, dict] = {}
        scrape_done = 0
        _ai_log(
            run_id, run_dir,
            f"Phase 1 scrape starting batches={len(groups)} concurrency={concurrency}",
        )
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(_scrape_one, idx, grp): idx
                for idx, grp in enumerate(groups, start=1)
            }
            for fut in as_completed(futures):
                rec = fut.result()
                scraped[rec["request_index"]] = rec
                scrapedo_seconds_total += rec["scrapedo_seconds"]
                if rec["error"]:
                    entities_without_scrape_data += len(rec["group"])
                scrape_done += 1
                status["batches_done"] = scrape_done
                status["scrapedo_request_count"] = scrape_done
                status["scrapedo_seconds_total"] = round(scrapedo_seconds_total, 3)
                status["entities_without_scrape_data"] = entities_without_scrape_data
                status["scrapedo_failed_requests"] = sum(
                    1 for r in scraped.values() if r["error"]
                )
                status["updated_at"] = utc_now_iso()
                _persist_status(run_id, run_dir, status)
        ordered = [scraped[i] for i in sorted(scraped)]
        ok_batches = [r for r in ordered if r["payload"] is not None]
        _ai_log(
            run_id, run_dir,
            f"Phase 1 scrape complete ok={len(ok_batches)} failed={len(ordered) - len(ok_batches)}",
        )

        # ------------------------------------------------------------- #
        # PHASE 2 - clean every scraped batch (sync OR Gemini Batch)
        # ------------------------------------------------------------- #
        status["phase"] = "cleaning"
        status["updated_at"] = utc_now_iso()
        _persist_status(run_id, run_dir, status)

        batch_results_by_index: dict[int, list[EntityResult]] = {}
        llm_error_by_index: dict[int, str | None] = {}
        llm_seconds_by_index: dict[int, float] = {}

        def _error_results(rec: dict, message: str) -> list[EntityResult]:
            return [
                EntityResult(
                    company_name=entity.company_name,
                    country=entity.country,
                    sno=entity.sno,
                    company_local_name=entity.company_local_name,
                    error=message,
                )
                for entity in rec["group"]
            ]

        def _messages_for(rec: dict) -> list[dict]:
            return build_cleanup_messages(_payload_text(rec["payload"]), rec["group"])

        if batch_mode:
            if not _str_env("GEMINI_API_KEY"):
                raise RuntimeError("GEMINI_API_KEY not configured (required for AI_MODE_LLM_BATCH)")
            shard_size = max(1, _int_env("GEMINI_BATCH_SHARD_SIZE", 5000))
            max_inflight = max(1, _int_env("GEMINI_BATCH_MAX_INFLIGHT", 5))
            poll_sec = max(5, _int_env("AI_MODE_BATCH_POLL_SEC", 15))
            timeout_sec = max(60, _int_env("AI_MODE_BATCH_TIMEOUT_SEC", 172800))
            clean_t0 = time.perf_counter()

            items: list[tuple[str, dict]] = []
            for rec in ok_batches:
                key = f"batch-{rec['request_index']:06d}"
                items.append((key, gemini_batch.messages_to_gemini_request(_messages_for(rec))))
            shards = [items[i : i + shard_size] for i in range(0, len(items), shard_size)]
            _ai_log(
                run_id, run_dir,
                f"Phase 2 Gemini batch: {len(items)} requests in {len(shards)} shard(s) "
                f"shard_size={shard_size} max_inflight={max_inflight} model={cfg.model}",
            )

            collected_by_key: dict[str, dict] = {}
            job_names: list[str] = list(status.get("gemini_batch_jobs") or [])
            next_shard = 0
            inflight: dict[str, int] = {}  # batch_name -> shard index
            deadline = time.monotonic() + timeout_sec
            while next_shard < len(shards) or inflight:
                while next_shard < len(shards) and len(inflight) < max_inflight:
                    si = next_shard
                    next_shard += 1
                    create_obj = gemini_batch.create_batch(
                        cfg.model, shards[si], display_name=f"ai-mode-{run_id}-shard-{si + 1}"
                    )
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
                                if c.get("key"):
                                    collected_by_key[c["key"]] = c
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

            for rec in ok_batches:
                idx = rec["request_index"]
                key = f"batch-{idx:06d}"
                c = collected_by_key.get(key)
                if c is None or c.get("error") or not c.get("text"):
                    msg = sanitize_secret_text(
                        "missing from LLM batch output"
                        if c is None
                        else f"LLM error: {c.get('error')}"
                    )
                    llm_error_by_index[idx] = msg
                    batch_results_by_index[idx] = _error_results(rec, msg)
                    llm_errors += len(rec["group"])
                    continue
                parsed_array = parse_json_array_from_text(c["text"])
                if parsed_array is None:
                    msg = sanitize_secret_text("LLM error: could not parse JSON array from batch output")
                    llm_error_by_index[idx] = msg
                    batch_results_by_index[idx] = _error_results(rec, msg)
                    llm_errors += len(rec["group"])
                    continue
                usage_total = usage_total + parse_gemini_usage(c.get("usage"))
                batch_results_by_index[idx] = parse_cleanup_response(parsed_array, rec["group"])
            llm_seconds_total = time.perf_counter() - clean_t0
        else:
            for rec in ok_batches:
                idx = rec["request_index"]
                messages = _messages_for(rec)
                t0 = time.perf_counter()
                try:
                    parsed, usage = llm.complete_json(messages)
                    secs = time.perf_counter() - t0
                    usage_total = usage_total + usage
                    parsed_array = coerce_json_array(parsed)
                    if parsed_array is None:
                        msg = "LLM error: response was not a JSON array"
                        llm_error_by_index[idx] = msg
                        batch_results_by_index[idx] = _error_results(rec, msg)
                        llm_errors += len(rec["group"])
                    else:
                        batch_results_by_index[idx] = parse_cleanup_response(
                            parsed_array, rec["group"]
                        )
                except Exception as exc:
                    secs = time.perf_counter() - t0
                    msg = sanitize_secret_text(f"LLM error: {exc}")
                    llm_error_by_index[idx] = msg
                    batch_results_by_index[idx] = _error_results(rec, msg)
                    llm_errors += len(rec["group"])
                llm_seconds_by_index[idx] = secs
                llm_seconds_total += secs
                status["llm_errors"] = llm_errors
                status["updated_at"] = utc_now_iso()
                _persist_status(run_id, run_dir, status)

        # ------------------------------------------------------------- #
        # PHASE 3 - assemble results + per-request records (in order)
        # ------------------------------------------------------------- #
        for rec in ordered:
            idx = rec["request_index"]
            if rec["payload"] is None:
                results.extend(_error_results(rec, f"scrape.do error: {rec['error']}"))
                per_request_records.append(
                    {
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
                )
                continue
            results.extend(batch_results_by_index.get(idx, []))
            rec_error = llm_error_by_index.get(idx)
            llm_secs = llm_seconds_by_index.get(idx, 0.0)
            per_request_records.append(
                {
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
            )

        # ----------------------------------------------------------------- #
        # Re-run carryover (Task 17): merge the previous run's successes into
        # this run's outputs. Carried entities count toward websites_found
        # (the run's found.csv really contains those URLs) but NOT toward
        # scrapedo/llm request or token stats; the summary exposes a separate
        # ``carried_over`` count so the split stays visible.
        # ----------------------------------------------------------------- #
        carried_over = 0
        carryover_path = run_dir / "carryover.json"
        if carryover_path.exists():
            try:
                carried_objs = json.loads(carryover_path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                carried_objs = []
                _ai_log(
                    run_id, run_dir,
                    f"carryover.json unreadable; ignoring ({exc})", logging.WARNING,
                )
            for obj in carried_objs:
                if not isinstance(obj, dict):
                    continue
                result = EntityResult.from_llm_object(obj)
                # Chained reruns: the entity may already carry a carried_over
                # flag from an earlier run — don't stack duplicates.
                if not any(f.flag == "carried_over" for f in result.flags):
                    result.flags.append(Flag("carried_over", "from previous run"))
                results.append(result)
                carried_over += 1
            if carried_over:
                _ai_log(
                    run_id, run_dir,
                    f"Merged {carried_over} carried-over success(es) from previous run",
                )

        # ----------------------------------------------------------------- #
        # Outputs: found.csv / notFound.csv / ONE final_report.json
        # ----------------------------------------------------------------- #
        total_wall = time.perf_counter() - wall_t0
        completed_at = utc_now_iso()
        failed_request_count = _failed_request_count(per_request_records)
        websites_found = sum(1 for r in results if r.website_url)
        websites_not_found = len(results) - websites_found
        run_status = "completed_with_errors" if failed_request_count else "completed"

        # Per-run cost (Task 15): LLM tokens priced via env rates + scrape.do
        # per-request credits from response headers (env-rate estimate fallback).
        llm_usd = calculate_llm_cost_usd(
            provider=cfg.provider,
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
            "llm": {"provider": cfg.provider, "base_url": cfg.base_url, "model": cfg.model},
            "batch_size": batch_size,
            "total_input_entities": len(entities),
            "entities_processed": len(results),
            "entities_without_scrape_data": entities_without_scrape_data,
            "llm_errors": llm_errors,
            "websites_found": websites_found,
            "websites_not_found": websites_not_found,
            "carried_over": carried_over,
            "scrapedo_request_count": len(per_request_records),
            "failed_request_count": failed_request_count,
            "scrapedo_failed_requests": _scrapedo_failed_request_count(per_request_records),
            "token_usage": asdict(usage_total),
            "cost": cost,
            "scrapedo_seconds_total": round(scrapedo_seconds_total, 3),
            "llm_seconds_total": round(llm_seconds_total, 3),
            "batch_duration_seconds": round(total_wall, 3),
            "started_at": started_at,
            "completed_at": completed_at,
        }
        output_paths = write_outputs(run_dir, results, summary=summary, requests=per_request_records)

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
        status["entities_processed"] = len(results)
        status["entities_without_scrape_data"] = entities_without_scrape_data
        status["llm_errors"] = llm_errors
        status["scrapedo_request_count"] = len(per_request_records)
        status["failed_request_count"] = failed_request_count
        status["scrapedo_failed_requests"] = _scrapedo_failed_request_count(per_request_records)
        status["websites_found"] = websites_found
        status["websites_not_found"] = websites_not_found
        status["carried_over"] = carried_over
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
        file_links = {name: str(path) for name, path in output_paths.items()}
        file_links["input.csv"] = str(input_csv)
        file_links["run.log"] = str(run_log)
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

    except Exception as exc:  # never raise to caller
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
        return
