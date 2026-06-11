"""AI Mode orchestrator.

Drives the AI Mode pipeline modules (scrape.do Google AI Mode ->
LLM cleanup pipeline) and writes results under ``ai_mode_result/<run_id>/``.

This module is a pure-sync orchestration layer. ``run_ai_mode_sync`` is intended
to be invoked from a thread (e.g. ``asyncio.to_thread``); it never raises to the
caller, instead reflecting any failure in the run's ``status.json``.

Public API (other modules depend on these names/signatures):
    prepare_ai_mode_run(raw_csv, filename) -> dict
    run_ai_mode_sync(run_id) -> None
    list_ai_mode_runs() -> list[dict]
    get_ai_mode_status(run_id) -> dict
    get_ai_mode_result_path(run_id, file_name) -> Path
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from app.services.ai_mode.company_csv_loader import load_company_entities
from app.services.ai_mode.company_extraction import build_company_messages, parse_company_results
from app.services.ai_mode.company_prompting import build_company_search_query
from app.services.ai_mode.company_reporting import build_company_report_dict, write_company_outputs
from app.services.ai_mode.extraction import build_messages, parse_results
from app.services.ai_mode.llm_client import make_llm_client, parse_gemini_usage
from app.services.ai_mode.models import (
    CompanyCleanResult,
    EntityCleanResult,
    EntityInput,
    TokenUsage,
    utc_now_iso,
)
from app.services.ai_mode.prompting import build_search_query, chunked
from app.services.ai_mode.cleanup_reporting import build_report_dict, write_outputs
from app.services.ai_mode.scrapedo_client import ScrapeDoClient
from app.services.ai_mode.settings import DEFAULT_LLM_BASE_URLS, LLMConfig, Settings

from app.services.ai_mode import gemini_batch
from app.core.config import LEGACY_AI_MODE_RESULT_DIR, PROMPTS_DIR


AI_MODE_RESULT_DIR = LEGACY_AI_MODE_RESULT_DIR
ALLOWED_RESULT_FILES = {
    "final_report.json",
    "report.json",
    "found.csv",
    "notFound.csv",
    "run.log",
    "ai_mode_debug.log",
    "input.csv",
}

# Prompt templates.
_ADDR_PROMPT_PATH = PROMPTS_DIR / "search_query_template.txt"
_COMPANY_PROMPT_PATH = PROMPTS_DIR / "company_search_template.txt"

# Header sets used by the flexible address loader. Entries are matched against
# headers normalized by ``_normalize_header`` (lowercase, strip, internal
# whitespace collapsed to underscores), so e.g. "Legal Name" -> "legal_name".
_NAME_HEADERS = {"entity_name", "company_name", "company", "name", "legal_name", "entity"}
_COUNTRY_HEADERS = {"country", "country_name", "nation", "country_code"}
_ADDRESS_HEADERS = {"address", "input_full_address", "full_address", "fulladdress"}
_FIRM_ID_HEADERS = {"firm_id", "firmid", "id"}

# In-memory write-through cache of run status dicts.
_RUNS: dict[str, dict] = {}
_AI_MODE_LOGGER = logging.getLogger("ai_mode")

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


def _ensure_ai_mode_logger() -> logging.Logger:
    if not _AI_MODE_LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        _AI_MODE_LOGGER.addHandler(handler)
    level_name = os.getenv("AI_MODE_LOG_LEVEL", "INFO").strip().upper()
    _AI_MODE_LOGGER.setLevel(getattr(logging, level_name, logging.INFO))
    _AI_MODE_LOGGER.propagate = False
    return _AI_MODE_LOGGER


def sanitize_secret_text(value: str) -> str:
    """Redact Scrape.do token query parameters in user-facing output."""
    return re.sub(r"([?&]token=)[^&'\"\\s]+", r"\1[REDACTED]", value)


def sanitize_for_response(value: Any) -> Any:
    """Recursively redact secrets before returning JSON through the UI API."""
    if isinstance(value, str):
        return sanitize_secret_text(value)
    if isinstance(value, list):
        return [sanitize_for_response(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_for_response(item) for key, item in value.items()}
    return value


def _debug_log_path(run_dir: Path) -> Path:
    return run_dir / "ai_mode_debug.log"


def _ai_log(run_id: str, run_dir: Path, message: str, level: int = logging.INFO) -> None:
    safe_message = sanitize_secret_text(message)
    _ensure_ai_mode_logger().log(level, "[run:%s] %s", run_id, safe_message)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        with _debug_log_path(run_dir).open("a", encoding="utf-8") as handle:
            handle.write(f"{utc_now_iso()} {logging.getLevelName(level)} {safe_message}\n")
    except OSError:
        _ensure_ai_mode_logger().warning(
            "[run:%s] failed to write AI Mode debug log", run_id
        )


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


def _query_debug_stats(client: ScrapeDoClient, query: str, extra_params: dict[str, str] | None = None) -> dict[str, int]:
    base_url = "https://api.scrape.do/plugin/google/search/ai-mode"
    params = client.build_params(query, extra_params=extra_params)
    encoded_url_chars = len(base_url) + 1 + len(urlencode(params))
    return {
        "query_chars": len(query),
        "encoded_url_chars": encoded_url_chars,
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

    Reads website_url_finder's own process env (app.py has already loaded .env);
    does NOT load any scrape.do .env file.
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
# CSV helpers
# --------------------------------------------------------------------------- #
def _normalize_header(key: str | None) -> str:
    """Lowercase, strip, and collapse internal whitespace to single underscores.

    This lets human-friendly headers ("Legal Name", "Country Code") match the
    underscore-style lookup keys used by the flexible address loader.
    """
    normalized = (key or "").strip().lower()
    return "_".join(normalized.split())


def _normalize_row(row: dict[str, Any]) -> dict[str, str]:
    """Lowercase/strip keys; coerce values to stripped strings."""
    normalized: dict[str, str] = {}
    for key, value in row.items():
        nkey = _normalize_header(key)
        if not nkey:
            continue
        if value is None:
            text = ""
        elif isinstance(value, list):
            text = " ".join(str(part) for part in value if part is not None).strip()
        else:
            text = str(value).strip()
        # Keep the first non-empty mapping for a given normalized key.
        if nkey not in normalized or (not normalized[nkey] and text):
            normalized[nkey] = text
    return normalized


def _first_match(row: dict[str, str], headers: set[str]) -> str:
    for key in headers:
        value = row.get(key, "")
        if value:
            return value
    return ""


def _detect_input_type(fieldnames: list[str] | None) -> str:
    # "Company Name ENG" normalizes to "company_name_eng".
    for name in fieldnames or []:
        if _normalize_header(name) == "company_name_eng":
            return "company"
    return "address"


def load_address_entities(csv_path: Path) -> list[EntityInput]:
    """Load address-mode entities from a CSV with flexible header mapping.

    Rows with an empty entity name are skipped. ``row_number`` enumerates from 2
    (accounting for the header row).
    """
    entities: list[EntityInput] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_number, raw_row in enumerate(reader, start=2):
            row = _normalize_row(raw_row)
            entity_name = _first_match(row, _NAME_HEADERS)
            if not entity_name:
                continue
            country = _first_match(row, _COUNTRY_HEADERS)
            address = _first_match(row, _ADDRESS_HEADERS)
            firm_id_raw = _first_match(row, _FIRM_ID_HEADERS)
            firm_id = int(firm_id_raw) if firm_id_raw.isdigit() else None
            entities.append(
                EntityInput(
                    entity_name=entity_name,
                    country=country,
                    address=address,
                    firm_id=firm_id,
                    row_number=row_number,
                )
            )
    return entities


# --------------------------------------------------------------------------- #
# Status persistence + accessors
# --------------------------------------------------------------------------- #
def _run_dir(run_id: str) -> Path:
    return AI_MODE_RESULT_DIR / run_id


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
    """Reflect request-level failures from report.json in the UI status."""
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
        status["error"] = f"{failed_request_count} request(s) failed. See report.json."
    return status


def _persist_status(run_id: str, status: dict) -> None:
    """Write ``status.json`` for the run and update the in-memory cache."""
    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    status = _reconcile_status_from_report(run_dir, status)
    status["available_files"] = _available_files(run_dir)
    (run_dir / "status.json").write_text(
        json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _RUNS[run_id] = status


def _read_status(run_id: str) -> dict:
    """Load status.json, falling back to the in-memory cache."""
    run_dir = _run_dir(run_id)
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


def get_ai_mode_status(run_id: str) -> dict:
    """Return the current status dict for a run.

    Raises KeyError if the run is unknown. Refreshes ``available_files``.
    """
    run_dir = _run_dir(run_id)
    if not run_dir.exists():
        raise KeyError(run_id)
    status = _read_status(run_id)
    status = _reconcile_status_from_report(run_dir, status)
    status["available_files"] = _available_files(run_dir)
    _RUNS[run_id] = status
    _persist_status(run_id, status)
    return status


def list_ai_mode_runs() -> list[dict]:
    """Return all known runs, newest first (by created_at)."""
    runs: list[dict] = []
    if not AI_MODE_RESULT_DIR.exists():
        return runs
    for status_path in AI_MODE_RESULT_DIR.glob("*/status.json"):
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        status = _reconcile_status_from_report(status_path.parent, status)
        status["available_files"] = _available_files(status_path.parent)
        if status.get("status") == "completed_with_errors":
            _persist_status(str(status.get("run_id") or status_path.parent.name), status)
        runs.append(status)
    runs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return runs


def get_ai_mode_result_path(run_id: str, file_name: str) -> Path:
    """Resolve a result file path for a run.

    Raises KeyError for an unknown run, ValueError for a disallowed file name, and
    FileNotFoundError when the file does not exist.
    """
    run_dir = _run_dir(run_id)
    if not run_dir.exists():
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
def prepare_ai_mode_run(raw_csv: bytes, filename: str) -> dict:
    """Validate an uploaded CSV, register a queued run, and persist initial state.

    Detects the input type (company vs address), counts usable rows, stores the
    raw CSV as input.csv, and writes a ``queued`` status.json. Does NOT call any
    external API.
    """
    if not (filename or "").lower().endswith(".csv"):
        raise ValueError("Only .csv file is supported.")

    text = raw_csv.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames
    input_type = _detect_input_type(fieldnames)

    total_rows = 0
    for raw_row in reader:
        if input_type == "company":
            name = (raw_row.get("Company Name ENG") or "").strip()
            if name:
                total_rows += 1
        else:
            row = _normalize_row(raw_row)
            if _first_match(row, _NAME_HEADERS):
                total_rows += 1

    if total_rows == 0:
        raise ValueError("No usable rows found in the CSV.")

    run_id = uuid.uuid4().hex
    run_dir = _run_dir(run_id)
    (run_dir / "raw_scrapedo_response").mkdir(parents=True, exist_ok=True)
    (run_dir / "input.csv").write_bytes(raw_csv)

    # Build the LLM config only to surface provider/model labels (no API call).
    llm_config = build_ai_mode_llm_config()
    batch_size = _int_env("SCRAPEDO_BATCH_SIZE", 10)

    now = utc_now_iso()
    status = {
        "run_id": run_id,
        "status": "queued",
        "input_type": input_type,
        "total_rows": total_rows,
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
    _persist_status(run_id, status)
    _ai_log(
        run_id,
        run_dir,
        f"AI Mode run prepared filename={filename or '-'} input_type={input_type} "
        f"total_rows={total_rows} batch_size={batch_size} llm_provider={llm_config.provider} "
        f"llm_model={llm_config.model}",
    )

    return {
        "run_id": run_id,
        "total_rows": total_rows,
        "input_type": input_type,
        "llm_provider": llm_config.provider,
        "llm_model": llm_config.model,
        "batch_size": batch_size,
        "created_at": now,
        "status_url": f"/uploads/ai-mode/{run_id}/status",
        "result_url": f"/uploads/ai-mode/{run_id}/result",
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
    run_dir = _run_dir(run_id)
    status = _read_status(run_id)
    started_at = utc_now_iso()
    wall_t0 = time.perf_counter()
    debug_log = _debug_log_path(run_dir)
    if debug_log.exists():
        try:
            debug_log.unlink()
        except OSError:
            pass
    _ai_log(run_id, run_dir, "AI Mode run starting")

    status["status"] = "running"
    status["started_at"] = started_at
    status["updated_at"] = utc_now_iso()
    status["error"] = None
    _persist_status(run_id, status)

    try:
        settings = build_ai_mode_settings()
        cfg = build_ai_mode_llm_config()
        _ai_log(
            run_id,
            run_dir,
            "Settings loaded "
            f"batch_size={settings.batch_size} max_query_chars={settings.scrapedo_max_query_chars} "
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
            log=lambda message: _ai_log(run_id, run_dir, message),
        )

        input_type = status.get("input_type") or _detect_input_type_from_status(run_dir)
        is_company = input_type == "company"

        input_csv = run_dir / "input.csv"
        if is_company:
            entities = load_company_entities(input_csv)
        else:
            entities = load_address_entities(input_csv)
        _ai_log(
            run_id,
            run_dir,
            f"Loaded entities input_type={input_type} total_entities={len(entities)} input_csv={input_csv}",
        )

        groups = list(chunked(entities, settings.batch_size))
        _ai_log(
            run_id,
            run_dir,
            f"Created {len(groups)} Scrape.do batch(es) from batch_size={settings.batch_size}",
        )
        status["batches_total"] = len(groups)
        status["updated_at"] = utc_now_iso()
        _persist_status(run_id, status)

        raw_dir = run_dir / "raw_scrapedo_response"
        raw_dir.mkdir(parents=True, exist_ok=True)

        results: list = []
        per_request_records: list[dict] = []
        usage_total = TokenUsage()
        entities_without_scrape_data = 0
        llm_errors = 0
        scrapedo_seconds_total = 0.0
        llm_seconds_total = 0.0
        sno = 0

        batch_mode = _bool_env("AI_MODE_LLM_BATCH", False)
        concurrency = max(1, _int_env("SCRAPEDO_CONCURRENCY", 5))

        # ------------------------------------------------------------- #
        # PHASE 1 - scrape every batch (parallel, bounded by concurrency)
        # ------------------------------------------------------------- #
        status["phase"] = "scraping"
        status["updated_at"] = utc_now_iso()
        _persist_status(run_id, status)

        def _scrape_one(request_index: int, group: list) -> dict:
            group_names = [e.entity_name for e in group]
            if is_company:
                query = build_company_search_query(group, prompt_path=_COMPANY_PROMPT_PATH)
            else:
                query = build_search_query(group, prompt_path=_ADDR_PROMPT_PATH)
            geo_params, geo_debug = _geo_params_for_group(group, settings)
            raw_name = f"request_{request_index:03d}.json"
            raw_path = raw_dir / raw_name
            rel_raw_path = f"raw_scrapedo_response/{raw_name}"
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
                payload = scrapedo_client.search_google_ai_mode(query, extra_params=geo_params)
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
                _persist_status(run_id, status)
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
        _persist_status(run_id, status)

        batch_results_by_index: dict[int, list] = {}
        llm_error_by_index: dict[int, str | None] = {}
        llm_seconds_by_index: dict[int, float] = {}

        def _error_results(rec: dict, message: str) -> list:
            out: list = []
            if is_company:
                for entity in rec["group"]:
                    out.append(
                        CompanyCleanResult(
                            company_name_eng=entity.company_name_eng,
                            company_name_local=entity.company_name_local,
                            country_code=entity.country_code,
                            error=message,
                        )
                    )
            else:
                for name in rec["group_names"]:
                    out.append(EntityCleanResult(entity_name=name, error=message))
            return out

        def _messages_for(rec: dict):
            payload = rec["payload"]
            text_blocks = payload.get("text_blocks") if isinstance(payload, dict) else None
            references = payload.get("references") if isinstance(payload, dict) else None
            if is_company:
                return build_company_messages(rec["group"], text_blocks, references)
            return build_messages(rec["group_names"], text_blocks, references)

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
                    _persist_status(run_id, status)
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
                parsed = gemini_batch.parse_json_from_text(c["text"])
                if parsed is None:
                    msg = sanitize_secret_text("LLM error: could not parse JSON from batch output")
                    llm_error_by_index[idx] = msg
                    batch_results_by_index[idx] = _error_results(rec, msg)
                    llm_errors += len(rec["group"])
                    continue
                usage_total = usage_total + parse_gemini_usage(c.get("usage"))
                if is_company:
                    batch_results_by_index[idx] = parse_company_results(parsed, rec["group"])
                else:
                    batch_results_by_index[idx] = parse_results(parsed, rec["group_names"])
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
                    if is_company:
                        batch_results_by_index[idx] = parse_company_results(parsed, rec["group"])
                    else:
                        batch_results_by_index[idx] = parse_results(parsed, rec["group_names"])
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
                _persist_status(run_id, status)

        # ------------------------------------------------------------- #
        # PHASE 3 - assemble results + per-request records (in order)
        # ------------------------------------------------------------- #
        for rec in ordered:
            idx = rec["request_index"]
            if rec["payload"] is None:
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
            batch_results = batch_results_by_index.get(idx, [])
            if not is_company:
                for entity, result in zip(rec["group"], batch_results):
                    result.location = entity.address
                    result.country = entity.country
            for result in batch_results:
                sno += 1
                result.sno = sno
            results.extend(batch_results)
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

        status["batches_done"] = len(groups)
        status["entities_processed"] = len(results)
        status["entities_without_scrape_data"] = entities_without_scrape_data
        status["llm_errors"] = llm_errors
        status["scrapedo_request_count"] = len(per_request_records)
        status["failed_request_count"] = _failed_request_count(per_request_records)
        status["scrapedo_failed_requests"] = _scrapedo_failed_request_count(per_request_records)
        status["scrapedo_seconds_total"] = round(scrapedo_seconds_total, 3)
        status["llm_seconds_total"] = round(llm_seconds_total, 3)
        status["token_usage"] = asdict(usage_total)
        status["updated_at"] = utc_now_iso()
        _persist_status(run_id, status)

        # ----------------------------------------------------------------- #
        # Final report
        # ----------------------------------------------------------------- #
        total_wall = time.perf_counter() - wall_t0
        completed_at = utc_now_iso()
        llm_label = {"provider": cfg.provider, "base_url": cfg.base_url, "model": cfg.model}

        if is_company:
            report = build_company_report_dict(
                source_batch_dir=str(run_dir),
                generated_at=utc_now_iso(),
                llm=llm_label,
                results=results,
                total_input_entities=len(entities),
                entities_without_scrape_data=entities_without_scrape_data,
                llm_errors=llm_errors,
                token_usage=usage_total,
                time_taken_seconds=total_wall,
            )
        else:
            report = build_report_dict(
                source_batch_dir=str(run_dir),
                generated_at=utc_now_iso(),
                llm=llm_label,
                results=results,
                total_input_entities=len(entities),
                entities_without_scrape_data=entities_without_scrape_data,
                llm_errors=llm_errors,
                token_usage=usage_total,
                time_taken_seconds=total_wall,
            )

        # Enrich the report with run-level metadata + timing.
        report["run_id"] = run_id
        report["input_type"] = input_type
        report["requests"] = per_request_records
        report["summary"]["scrapedo_seconds_total"] = round(scrapedo_seconds_total, 3)
        report["summary"]["llm_seconds_total"] = round(llm_seconds_total, 3)
        report["summary"]["batch_duration_seconds"] = round(total_wall, 3)
        report["summary"]["started_at"] = started_at
        report["summary"]["completed_at"] = completed_at

        if is_company:
            write_company_outputs(run_dir, report, results)
        else:
            write_outputs(run_dir, report, results)

        # Separate per-request timing metadata file.
        report_meta = {
            "run_id": run_id,
            "input_type": input_type,
            "batch_size": settings.batch_size,
            "total_entities": len(entities),
            "failed_request_count": _failed_request_count(per_request_records),
            "scrapedo_failed_requests": _scrapedo_failed_request_count(per_request_records),
            "requests": per_request_records,
            "started_at": started_at,
            "completed_at": completed_at,
            "scrapedo_seconds_total": round(scrapedo_seconds_total, 3),
            "llm_seconds_total": round(llm_seconds_total, 3),
        }
        (run_dir / "report.json").write_text(
            json.dumps(report_meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Human-readable per-request log.
        log_lines = []
        for record in per_request_records:
            line = (
                f"{record['request_index']} status={record['status']} "
                f"scrapedo={record['scrapedo_seconds']}s "
                f"llm={record['llm_seconds']}s "
                f"entities={record['entity_count']}"
            )
            if record.get("error"):
                line += f" {record['error']}"
            log_lines.append(line)
        (run_dir / "run.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        _ai_log(
            run_id,
            run_dir,
            f"Wrote outputs final_report.json report.json run.log found.csv notFound.csv "
            f"requests={len(per_request_records)} failed_requests={_failed_request_count(per_request_records)}",
        )

        # Reflect final summary counts in the status.
        summary = report.get("summary", {})
        failed_request_count = _failed_request_count(per_request_records)
        status["status"] = "completed_with_errors" if failed_request_count else "completed"
        status["entities_processed"] = summary.get("entities_processed", len(results))
        status["entities_without_scrape_data"] = entities_without_scrape_data
        status["llm_errors"] = llm_errors
        status["scrapedo_request_count"] = len(per_request_records)
        status["failed_request_count"] = failed_request_count
        status["scrapedo_failed_requests"] = _scrapedo_failed_request_count(per_request_records)
        status["websites_found"] = summary.get("websites_found", 0)
        status["websites_not_found"] = summary.get("websites_not_found", 0)
        status["scrapedo_seconds_total"] = round(scrapedo_seconds_total, 3)
        status["llm_seconds_total"] = round(llm_seconds_total, 3)
        status["batch_duration_seconds"] = round(total_wall, 3)
        status["token_usage"] = asdict(usage_total)
        status["completed_at"] = completed_at
        status["updated_at"] = utc_now_iso()
        status["error"] = (
            f"{failed_request_count} request(s) failed. See report.json."
            if failed_request_count
            else None
        )
        _persist_status(run_id, status)
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
        _persist_status(run_id, status)
        _ai_log(
            run_id,
            run_dir,
            f"AI Mode run crashed error={status['error']}",
            logging.ERROR,
        )
        return


def _detect_input_type_from_status(run_dir: Path) -> str:
    """Fallback input-type detection from the stored input.csv header."""
    input_csv = run_dir / "input.csv"
    if not input_csv.exists():
        return "address"
    try:
        with input_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            return _detect_input_type(reader.fieldnames)
    except (OSError, csv.Error):
        return "address"
