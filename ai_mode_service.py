"""AI Mode orchestrator.

Drives the vendored ``scrapedo_finder`` package (scrape.do Google AI Mode ->
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
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scrapedo_finder.company_csv_loader import load_company_entities
from scrapedo_finder.company_extraction import build_company_messages, parse_company_results
from scrapedo_finder.company_prompting import build_company_search_query
from scrapedo_finder.company_reporting import build_company_report_dict, write_company_outputs
from scrapedo_finder.extraction import build_messages, parse_results
from scrapedo_finder.llm_client import make_llm_client
from scrapedo_finder.models import (
    CompanyCleanResult,
    EntityCleanResult,
    EntityInput,
    TokenUsage,
    utc_now_iso,
)
from scrapedo_finder.prompting import build_search_query, chunked
from scrapedo_finder.cleanup_reporting import build_report_dict, write_outputs
from scrapedo_finder.scrapedo_client import ScrapeDoClient
from scrapedo_finder.settings import DEFAULT_LLM_BASE_URLS, LLMConfig, Settings


REPO_ROOT = Path(__file__).resolve().parent
AI_MODE_RESULT_DIR = REPO_ROOT / "ai_mode_result"
ALLOWED_RESULT_FILES = {
    "final_report.json",
    "report.json",
    "found.csv",
    "notFound.csv",
    "run.log",
    "input.csv",
}

# Prompt templates that ship with the vendored package.
_ADDR_PROMPT_PATH = REPO_ROOT / "scrapedo_finder" / "prompts" / "search_query_template.txt"
_COMPANY_PROMPT_PATH = REPO_ROOT / "scrapedo_finder" / "prompts" / "company_search_template.txt"

# Header sets used by the flexible address loader. Entries are matched against
# headers normalized by ``_normalize_header`` (lowercase, strip, internal
# whitespace collapsed to underscores), so e.g. "Legal Name" -> "legal_name".
_NAME_HEADERS = {"entity_name", "company_name", "company", "name", "legal_name", "entity"}
_COUNTRY_HEADERS = {"country", "country_name", "nation", "country_code"}
_ADDRESS_HEADERS = {"address", "input_full_address", "full_address", "fulladdress"}
_FIRM_ID_HEADERS = {"firm_id", "firmid", "id"}

# In-memory write-through cache of run status dicts.
_RUNS: dict[str, dict] = {}


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
    provider = (_str_env("AI_MODE_LLM_PROVIDER", "gemini").lower()) or "gemini"
    if provider == "openai":
        api_key = _str_env("OPENAI_API_KEY")
        model = _str_env("OPENAI_MODEL") or "gpt-4o-mini"
        base_url = _str_env("OPENAI_BASE_URL") or DEFAULT_LLM_BASE_URLS["openai"]
    else:
        provider = "gemini"
        api_key = _str_env("GEMINI_API_KEY")
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


def _persist_status(run_id: str, status: dict) -> None:
    """Write ``status.json`` for the run and update the in-memory cache."""
    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
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
    status["available_files"] = _available_files(run_dir)
    _RUNS[run_id] = status
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
        status["available_files"] = _available_files(status_path.parent)
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
        "scrapedo_seconds_total": 0.0,
        "llm_seconds_total": 0.0,
        "token_usage": asdict(TokenUsage()),
        "created_at": now,
        "updated_at": now,
        "error": None,
    }
    _persist_status(run_id, status)

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

    status["status"] = "running"
    status["started_at"] = started_at
    status["updated_at"] = utc_now_iso()
    status["error"] = None
    _persist_status(run_id, status)

    try:
        settings = build_ai_mode_settings()
        cfg = build_ai_mode_llm_config()
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
        )

        input_type = status.get("input_type") or _detect_input_type_from_status(run_dir)
        is_company = input_type == "company"

        input_csv = run_dir / "input.csv"
        if is_company:
            entities = load_company_entities(input_csv)
        else:
            entities = load_address_entities(input_csv)

        groups = list(chunked(entities, settings.batch_size))
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

        for request_index, group in enumerate(groups, start=1):
            group_names = [e.entity_name for e in group]
            req_error: str | None = None
            req_status = "success"
            rel_raw_path: str | None = None
            scrapedo_seconds = 0.0
            llm_seconds = 0.0
            payload: dict | None = None

            # PHASE 1 - scrape.do
            if is_company:
                query = build_company_search_query(group, prompt_path=_COMPANY_PROMPT_PATH)
            else:
                query = build_search_query(group, prompt_path=_ADDR_PROMPT_PATH)

            t0 = time.perf_counter()
            try:
                payload = scrapedo_client.search_google_ai_mode(query)
                scrapedo_seconds = time.perf_counter() - t0
                raw_name = f"request_{request_index:03d}.json"
                (raw_dir / raw_name).write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                rel_raw_path = f"raw_scrapedo_response/{raw_name}"
            except Exception as exc:  # scrape.do failure: skip LLM for this batch
                scrapedo_seconds = time.perf_counter() - t0
                req_error = str(exc)
                req_status = "error"
                entities_without_scrape_data += len(group)
                scrapedo_seconds_total += scrapedo_seconds
                per_request_records.append(
                    {
                        "request_index": request_index,
                        "entity_count": len(group),
                        "entity_names": group_names,
                        "status": req_status,
                        "error": req_error,
                        "scrapedo_seconds": round(scrapedo_seconds, 3),
                        "llm_seconds": round(llm_seconds, 3),
                        "combined_seconds": round(scrapedo_seconds + llm_seconds, 3),
                        "raw_json_file": rel_raw_path,
                    }
                )
                status["batches_done"] = request_index
                status["scrapedo_seconds_total"] = round(scrapedo_seconds_total, 3)
                status["llm_seconds_total"] = round(llm_seconds_total, 3)
                status["entities_without_scrape_data"] = entities_without_scrape_data
                status["llm_errors"] = llm_errors
                status["token_usage"] = asdict(usage_total)
                status["updated_at"] = utc_now_iso()
                _persist_status(run_id, status)
                continue

            scrapedo_seconds_total += scrapedo_seconds

            # PHASE 2 - LLM cleanup
            text_blocks = payload.get("text_blocks") if isinstance(payload, dict) else None
            references = payload.get("references") if isinstance(payload, dict) else None

            if is_company:
                messages = build_company_messages(group, text_blocks, references)
            else:
                messages = build_messages(group_names, text_blocks, references)

            t0 = time.perf_counter()
            try:
                parsed, usage = llm.complete_json(messages)
                llm_seconds = time.perf_counter() - t0
                usage_total = usage_total + usage
                if is_company:
                    batch_results = parse_company_results(parsed, group)
                else:
                    batch_results = parse_results(parsed, group_names)
            except Exception as exc:  # LLM failure: emit per-entity error results
                llm_seconds = time.perf_counter() - t0
                req_error = f"LLM error: {exc}"
                req_status = "error"
                llm_errors += len(group)
                batch_results = []
                if is_company:
                    for entity in group:
                        batch_results.append(
                            CompanyCleanResult(
                                company_name_eng=entity.company_name_eng,
                                company_name_local=entity.company_name_local,
                                country_code=entity.country_code,
                                error=f"LLM error: {exc}",
                            )
                        )
                else:
                    for name in group_names:
                        batch_results.append(
                            EntityCleanResult(
                                entity_name=name,
                                error=f"LLM error: {exc}",
                            )
                        )

            llm_seconds_total += llm_seconds

            # Address mode: backfill location/country from the input entity (by order).
            if not is_company:
                for entity, result in zip(group, batch_results):
                    result.location = entity.address
                    result.country = entity.country

            # Stable, incrementing serial numbers across the whole run.
            for result in batch_results:
                sno += 1
                result.sno = sno
            results.extend(batch_results)

            per_request_records.append(
                {
                    "request_index": request_index,
                    "entity_count": len(group),
                    "entity_names": group_names,
                    "status": req_status,
                    "error": req_error,
                    "scrapedo_seconds": round(scrapedo_seconds, 3),
                    "llm_seconds": round(llm_seconds, 3),
                    "combined_seconds": round(scrapedo_seconds + llm_seconds, 3),
                    "raw_json_file": rel_raw_path,
                }
            )

            status["batches_done"] = request_index
            status["entities_processed"] = len(results)
            status["entities_without_scrape_data"] = entities_without_scrape_data
            status["llm_errors"] = llm_errors
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

        # Reflect final summary counts in the status.
        summary = report.get("summary", {})
        status["status"] = "completed"
        status["entities_processed"] = summary.get("entities_processed", len(results))
        status["entities_without_scrape_data"] = entities_without_scrape_data
        status["llm_errors"] = llm_errors
        status["websites_found"] = summary.get("websites_found", 0)
        status["websites_not_found"] = summary.get("websites_not_found", 0)
        status["scrapedo_seconds_total"] = round(scrapedo_seconds_total, 3)
        status["llm_seconds_total"] = round(llm_seconds_total, 3)
        status["batch_duration_seconds"] = round(total_wall, 3)
        status["token_usage"] = asdict(usage_total)
        status["completed_at"] = completed_at
        status["updated_at"] = utc_now_iso()
        status["error"] = None
        _persist_status(run_id, status)

    except Exception as exc:  # never raise to caller
        status["status"] = "failed"
        status["error"] = str(exc)
        status["updated_at"] = utc_now_iso()
        _persist_status(run_id, status)
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
