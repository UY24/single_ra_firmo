"""SerpWow engine — the FastAPI ``app``, all upload/status/output/batch endpoints,
the RabbitMQ producer + consumer + worker loop, upload/state/persist machinery, the
S3 store, the Gemini-batch driver, and Supabase/Slack tracking.

This was the ~8.3k-line ``legacy_app.py`` monolith; the cohesive pipeline helpers were
extracted into focused sibling modules (all re-imported below for backward-compatible
``engine.<name>`` access used by tests/scripts):

  constants / schemas / row_logging   — pipeline ids, Pydantic models, per-row logging
  geo / cost / url_utils / address     — leaf helpers (locale, pricing, URLs, addresses)
  query_builders / gmaps_scoring       — search-query construction, Maps scoring
  serpwow_client / gemini_llm          — SerpWow HTTP + Gemini confidence/selection
  csv_input / output_export / reporting — I/O + reporting
  modes/{gsearch,gmaps,firmographics,full,common} — per-mode row executors
  scrapedo_maps_client                 — scrape.do Google Maps search (gmaps pipeline)

Shared, provider-agnostic helpers live in ``app/services/common/`` (text, env).
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
import os
import re
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape as xml_escape

import aio_pika
import boto3
import httpx
from botocore.config import Config as BotoConfig
from aiormq.exceptions import ChannelInvalidStateError
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.core.config import PROJECT_ROOT
from app.services.common.env import get_float_env, get_int_env, get_bool_env
from app.services.common.text import slugify_company
from app.services.serpwow import serpwow_client
from app.services.common import llm_batch
from app.services.serpwow import reporting
from app.services.serpwow.cost import (
    calculate_gemini_cost_usd,
    calculate_gemini_batch_cost_usd,
    calculate_serpwow_cost_usd,
)
from app.services.serpwow.geo import COUNTRY_GL_ALIASES, _country_to_gl
from app.services.serpwow.url_utils import (
    _normalized_domain,
    _normalize_url_for_compare,
    _candidate_domain_is_plausible_for_company,
    _official_website_looks_plausible,
    is_disallowed_official_url,
    canonicalize_official_url,
    dedupe_candidate_urls,
    _domain_from_url,
    _normalize_website_input,
)
from app.services.serpwow.output_export import (
    _sanitize_excel_text,
    _excel_col_name,
    _xlsx_cell_xml,
    build_upload_output_csv_bytes,
    build_upload_output_xlsx_bytes,
)
from app.services.serpwow.csv_input import (
    _normalize_header,
    _validate_canonical_upload_csv,
    parse_csv_rows,
    parse_firmographics_csv_rows,
)
from app.services.serpwow.address import (
    _extract_address_search_fragments,
    _extract_address_component,
    _marker_variants,
    _extract_locality_city_postal,
    _normalize_location_token,
    _dedupe_location_parts,
    _is_suspicious_city_state_value,
    _heuristic_city_state_from_full_address,
    _address_evidence_markers,
    _normalize_address_match_text,
    _marker_variants_for_match,
    _marker_matches_candidate,
    _is_address_aligned,
    _meaningful_company_tokens,
    _extract_address_numbers,
)
from app.services.serpwow.query_builders import (
    build_primary_search_query,
    build_industry_fallback_query,
    build_address_fallback_query,
    _append_unique_attempt_query,
    _company_name_variants,
    _looks_like_person_name,
    _extract_phase5_pivots_from_serpwow,
    build_investigative_search_queries,
    build_selected_phase_queries,
)
from app.services.serpwow.gmaps_scoring import (
    _score_gmaps_candidates,
    _select_best_gmaps_website,
    _gmaps_confidence_for_entry,
    _gmaps_confidence_block,
)
from app.services.serpwow.serpwow_client import (
    SERPWOW_API_URL,
    run_serpwow_search,
    _extract_official_website_from_serpwow,
    _serpwow_ai_overview_is_ambiguous,
    _is_listing_or_profile_result,
    _extract_official_website_candidates_from_serpwow,
)
from app.services.serpwow import outcomes as _outcomes
from app.services.serpwow.outcomes import categorize_http_error
from app.services.serpwow.gemini_llm import (
    _parse_json_from_text,
    _gemini_generate_content_json,
    analyze_with_gemini,
    build_ai_overview_prompt,
    standardize_ai_overview_with_gemini,
    parse_city_state_from_full_address_with_gemini,
    transliterate_inputs_with_gemini,
    classify_address_with_gemini,
    choose_final_website_with_gemini,
)
from app.services.serpwow.constants import (
    PIPELINE_FIRMOGRAPHICS,
    PIPELINE_GMAPS,
    PIPELINE_GSEARCH,
    PIPELINE_RELATIONSHIP,
    COST_SUMMARY_PIPELINES,
    REPORTING_PIPELINES,
)
from app.services.serpwow.schemas import FirmographicsRequest, CrawlResponse
from app.services.serpwow.row_logging import (
    _now_iso,
    _short_text,
    _pipeline_logs_enabled,
    _log_row_stage,
)
from app.services.serpwow.modes.common import (
    run_scrapedo_search_for_firmographics,
    run_gmaps_from_module,
)
from app.services.serpwow.modes.gsearch import execute_gsearch_lookup_for_worker
from app.services.serpwow.modes.gmaps import execute_gmaps_lookup
from app.services.serpwow.modes.firmographics import execute_firmographic_extraction


def load_local_env(env_path: str = ".env") -> None:
    if not os.path.exists(env_path):
        return

    try:
        with open(env_path, "r", encoding="utf-8") as env_file:
            for line in env_file:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'").strip('"')
                # Only fill keys that are truly missing. Tests may intentionally blank
                # credential vars (via tests/__init__.py) to prevent accidental hits to
                # real cloud endpoints; respect that by not re-populating blanked vars.
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass


load_local_env(str(PROJECT_ROOT / ".env"))
app = FastAPI(title="Single RA ISI API", version="2.0.0")

UPLOAD_BASE_DIR = Path("/tmp/single_ra_isi")
UPLOAD_BASE_DIR.mkdir(parents=True, exist_ok=True)
S3_PREFIX = "single_ra_isi"

upload_locks: dict[str, asyncio.Lock] = {}

def get_upload_lock(upload_id: str) -> asyncio.Lock:
    if upload_id not in upload_locks:
        upload_locks[upload_id] = asyncio.Lock()
    return upload_locks[upload_id]

rabbitmq_connection: Optional[aio_pika.abc.AbstractRobustConnection] = None
rabbitmq_channel: Optional[aio_pika.abc.AbstractChannel] = None
rabbitmq_exchange: Optional[aio_pika.abc.AbstractExchange] = None
rabbitmq_queue: Optional[aio_pika.abc.AbstractQueue] = None
rabbitmq_consumer_tasks: list[asyncio.Task] = []
rabbitmq_last_error: Optional[str] = None
rabbitmq_stop_event: Optional[asyncio.Event] = None
search_fetch_semaphore: Optional[asyncio.Semaphore] = None
gemini_batch_tasks: dict[str, asyncio.Task] = {}
gemini_batch_reconciler_task: Optional[asyncio.Task] = None
gemini_batch_reconciler_stop: Optional[asyncio.Event] = None
upload_active_rows: dict[str, set[int]] = {}
upload_active_rows_lock = asyncio.Lock()
upload_summaries_cache: dict[str, dict[str, Any]] = {}
_s3_run_prefix_cache: dict[str, str] = {}
gemini_batch_list_cache: list[dict[str, Any]] = []
gemini_batch_list_cache_fetched_at: float = 0.0
gemini_batch_list_error_cooldown_until: float = 0.0

s3_client = None


# CrawlRequest/FirmographicsRequest/CrawlResponse live in schemas.py; re-imported at the top.


# Pipeline id constants live in constants.py; re-imported at the top of this module.

_GSEARCH_RESULT_FILES = {"found.csv", "notFound.csv",
                         "confirmed_relation.csv", "notconfirmed_relation.csv",
                         # The rerun/refund list (gmaps + relationship): the rows that
                         # got no answer, with their original input columns so the file
                         # can be downloaded and uploaded straight back.
                         "retry.csv",
                         "skipped.csv", "report.json", "run.log",
                         "output.json", "state.json"}


def patch_aio_pika_connection_del() -> None:
    # aio_pika Connection.__del__ schedules self.close() via ensure_future().
    # During interpreter/thread shutdown (e.g., GC in asyncio_0), there may be
    # no current event loop, which raises RuntimeError and leaks a coroutine warning.
    # Guarding here keeps shutdown noise-free without changing normal close flow.
    try:
        import aio_pika.connection as aio_pika_connection_module
    except Exception:
        return

    connection_cls = getattr(aio_pika_connection_module, "Connection", None)
    if connection_cls is None:
        return
    if getattr(connection_cls, "_safe_del_patched", False):
        return

    def _safe_del(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            is_closed = bool(getattr(self, "is_closed", True))
        except Exception:
            is_closed = True
        if is_closed or loop.is_closed():
            return
        try:
            loop.create_task(self.close())
        except Exception:
            pass

    connection_cls.__del__ = _safe_del
    setattr(connection_cls, "_safe_del_patched", True)


patch_aio_pika_connection_del()


# Row-trace logging helpers (_now_iso/_short_text/_pipeline_logs_enabled/_log_row_stage)
# live in row_logging.py; re-imported at the top of this module.


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        # Accept both "Z" and "+00:00" UTC representations.
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


# Env readers live in common/env.py; kept under their private names here so the
# many module-qualified callers (legacy_app._get_int_env, main.py, tests) resolve.
_get_float_env = get_float_env
_get_int_env = get_int_env
_get_bool_env = get_bool_env


def _batch_postprocess_enabled_for(pipeline: str) -> bool:
    """Does THIS module's chunked row-batch driver handle the pipeline?

    Deliberately ``uses_shared_row_batch``, not ``batch_enabled``: relationship batches too,
    but through its own driver in relationship_runner, and answering "yes" here would seed a
    second duplicate job for it.
    """
    return llm_batch.uses_shared_row_batch(pipeline)


def _batch_postprocess_pending(state: dict[str, Any]) -> bool:
    """gsearch: True while the Gemini batch hasn't reached a terminal status.

    Used to DEFER the terminal completion side-effects (Supabase 'completed' sync,
    Slack ping, output-file finalize) until the batch is done, so a batch run
    reports completion once with final numbers — like AI Mode. The `full`
    pipeline is intentionally left unchanged.
    """
    pipe = str(state.get("pipeline") or "")
    if pipe != PIPELINE_GSEARCH:
        return False
    if not _batch_postprocess_enabled_for(pipe):
        return False
    gb = state.get("gemini_batch")
    return isinstance(gb, dict) and gb.get("status") in {
        "waiting_for_rows", "queued", "running", "cancel_requested",
    }


def _batch_deleted_by_user(state: dict[str, Any]) -> bool:
    batch = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
    return bool(state.get("batch_deleted_by_user_at") or batch.get("deleted_by_user_at"))


def _batch_generation(state: dict[str, Any]) -> int:
    batch = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
    try:
        return max(0, int(batch.get("generation") or 0))
    except (TypeError, ValueError):
        return 0








def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# Country->gl locale mapping lives in geo.py; re-imported at the top of this module.


def _upload_dir(upload_id: str, company_name: str = "") -> Path:
    safe = _company_slug(company_name) if company_name else ""
    path = (UPLOAD_BASE_DIR / safe / upload_id) if safe else (UPLOAD_BASE_DIR / upload_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _find_upload_dir(upload_id: str) -> Path:
    """Locate an existing upload directory (flat or nested layout). Does not create."""
    direct = UPLOAD_BASE_DIR / upload_id
    if direct.exists():
        return direct
    for child in UPLOAD_BASE_DIR.iterdir():
        if child.is_dir() and child.name != upload_id:
            nested = child / upload_id
            if nested.exists():
                return nested
    return direct


def _state_file(upload_id: str) -> Path:
    return _find_upload_dir(upload_id) / "state.json"


def _output_file(upload_id: str) -> Path:
    return _find_upload_dir(upload_id) / "output.json"


async def _mark_upload_row_active(upload_id: str, row_index: int) -> None:
    async with upload_active_rows_lock:
        rows = upload_active_rows.setdefault(upload_id, set())
        rows.add(int(row_index))


async def _mark_upload_row_inactive(upload_id: str, row_index: int) -> None:
    async with upload_active_rows_lock:
        rows = upload_active_rows.get(upload_id)
        if not rows:
            return
        rows.discard(int(row_index))
        if not rows:
            upload_active_rows.pop(upload_id, None)


async def _get_upload_active_row_count(upload_id: str) -> int:
    async with upload_active_rows_lock:
        rows = upload_active_rows.get(upload_id)
        return len(rows) if rows else 0


async def _get_rabbitmq_queue_depth() -> Optional[int]:
    # aio_pika's Queue.declare() takes no `passive` kwarg (it raised TypeError
    # and this probe silently returned None, disabling the drained-queue gate);
    # the passive probe must go through channel.declare_queue.
    if rabbitmq_channel is None:
        return None
    try:
        queue_name = os.getenv("RABBITMQ_QUEUE", "singleRA_search_jobs")
        probe = await rabbitmq_channel.declare_queue(queue_name, passive=True)
        count = getattr(getattr(probe, "declaration_result", None), "message_count", None)
        if count is None:
            return None
        return max(0, int(count))
    except Exception:
        return None


def _build_row_job_payload(upload_id: str, row: dict[str, Any], pipeline: str, phase: str = "all", upload_company_name: str = "") -> dict[str, Any]:
    return {
        "upload_id": upload_id,
        "row_index": int(row.get("row_index", 0) or 0),
        "company_name": str(row.get("company_name") or ""),
        "country": str(row.get("country") or ""),
        "firm_id": str(row.get("firm_id") or ""),
        "industry": str(row.get("industry") or ""),
        "full_address": str(row.get("full_address") or ""),
        "official_website": str(row.get("official_website") or ""),
        "pipeline": pipeline,
        "phase": phase,
        "uploaded_at": _now_iso(),
        "upload_company_name": upload_company_name,
    }


def _upload_s3_prefix(upload_id: str, company_name: str = "", pipeline: str = "") -> str:
    safe = _company_slug(company_name) if company_name else ""
    pipe = (pipeline or "").strip()
    if safe and pipe:
        return f"{safe}/{pipe}/{upload_id}"
    if safe:
        return f"{safe}/{upload_id}"
    return upload_id


def _remember_s3_run_prefix(upload_id: str, prefix: str) -> None:
    value = str(prefix or "").strip()
    if not value or "\n" in value or "\r" in value:
        return
    _s3_run_prefix_cache[upload_id] = value
    try:
        (_find_upload_dir(upload_id) / ".s3_prefix").write_text(
            value, encoding="utf-8")
    except Exception:
        pass


def _restore_s3_run_prefix(upload_id: str) -> Optional[str]:
    cached = str(_s3_run_prefix_cache.get(upload_id) or "").strip()
    if cached and "\n" not in cached and "\r" not in cached:
        return cached
    try:
        restored = (_find_upload_dir(upload_id) / ".s3_prefix").read_text(
            encoding="utf-8").strip()
    except Exception:
        return None
    if not restored or "\n" in restored or "\r" in restored:
        return None
    _s3_run_prefix_cache[upload_id] = restored
    return restored


def _resolved_upload_s3_prefix(
    upload_id: str,
    company_name: str = "",
    pipeline: str = "",
) -> str:
    return _restore_s3_run_prefix(upload_id) or _upload_s3_prefix(
        upload_id, company_name, pipeline)


def _state_s3_key(upload_id: str, company_name: str = "", pipeline: str = "") -> str:
    return f"{_resolved_upload_s3_prefix(upload_id, company_name, pipeline)}/state.json"


def _output_s3_key(upload_id: str, company_name: str = "", pipeline: str = "") -> str:
    return f"{_resolved_upload_s3_prefix(upload_id, company_name, pipeline)}/output.json"


def _batch_input_jsonl_s3_key(upload_id: str, company_name: str = "", pipeline: str = "") -> str:
    return f"{_resolved_upload_s3_prefix(upload_id, company_name, pipeline)}/gemini_batch_input.jsonl"


def _batch_output_json_s3_key(upload_id: str, company_name: str = "", pipeline: str = "") -> str:
    return f"{_resolved_upload_s3_prefix(upload_id, company_name, pipeline)}/gemini_batch_output.json"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON ATOMICALLY (temp file + os.replace).

    A plain write_text leaves a truncated file if the process dies mid-write, and for
    state.json that loses the WHOLE run's progress, not one row. os.replace is atomic
    within a filesystem, so a reader sees either the old file or the new one.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _company_slug(value: str) -> str:
    """Company folder slug, unified with AI Mode's slugify_company.

    Thin wrapper over the shared ``common.text.slugify_company`` so SerpWow and
    AI Mode share one company folder. (_safe_name is still used for per-row file
    NAMES.)
    """
    return slugify_company(value)


def _safe_name(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip())
    return text.strip("_") or "item"


def _write_error_dumps(upload_dir: Path, state: dict[str, Any]) -> dict[str, Path]:
    """Write a per-row debugging JSON for every row with a technical problem worth
    debugging: hard errors (outcome=="error") AND degraded-found rows (a found row
    whose per-row Gemini selection failed, carrying context.llm_error).

    Pure disk, sync, best-effort by design of its caller (this function itself
    raises on genuine I/O failure, but the caller wraps it). Returns
    {filename: path} for the files actually written; other rows produce no file.
    """
    paths: dict[str, Path] = {}
    rows = state.get("rows") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        is_hard_error = row.get("outcome") == _outcomes.OUTCOME_ERROR
        ctx = (row.get("result") or {}).get("context") or {}
        llm_error = ctx.get("llm_error")
        # A degraded-found row: not a hard error, but a real Gemini selection failure.
        is_degraded_found = (not is_hard_error) and bool(llm_error)
        if not is_hard_error and not is_degraded_found:
            continue
        row_index = row.get("row_index")
        company_name = str(row.get("company_name") or "")
        formatted_results = ctx.get("formatted_results") if isinstance(ctx.get("formatted_results"), list) else []
        phases = [
            {
                "phase": fr.get("phase"),
                "used": fr.get("success"),
                "error": serpwow_client.sanitize_serpwow_error_text(fr.get("error")),
                "status_code": fr.get("status_code"),
                "error_category": fr.get("error_category"),
            }
            for fr in formatted_results
            if isinstance(fr, dict)
        ]
        http_status = phases[0]["status_code"] if phases else None
        if is_degraded_found:
            data = {
                "row_index": row_index,
                "company_name": company_name,
                "error_source": _outcomes.SRC_GEMINI,
                "error_category": _outcomes.categorize_http_error(None, str(llm_error)),
                "error_detail": str(llm_error),
                "http_status": http_status,
                # Record the row's ACTUAL outcome so a reader sees this is a
                # degraded-found (candidate fallback), not a hard error.
                "outcome": row.get("outcome"),
                "phases": phases,
            }
        else:
            data = {
                "row_index": row_index,
                "company_name": company_name,
                "error_source": row.get("error_source"),
                "error_category": row.get("error_category"),
                "error_detail": (
                    serpwow_client.sanitize_serpwow_error_text(row.get("error"))
                    if row.get("error_source") == _outcomes.SRC_SERPWOW
                    else row.get("error")
                ),
                "http_status": http_status,
                "phases": phases,
            }
        errors_dir = Path(upload_dir) / "errors"
        errors_dir.mkdir(parents=True, exist_ok=True)
        idx = row_index if isinstance(row_index, int) else 0
        name = f"{idx:06d}_{_safe_name(company_name)}_error.json"
        path = errors_dir / name
        _write_json(path, data)
        paths[name] = path
    return paths


# SerpWow HTTP client + response extraction (run_serpwow_search,
# _extract_official_website_from_serpwow, candidates, ambiguity, listing checks)
# live in serpwow_client.py; re-imported at the top.












# Search-query construction (build_*_query, _company_name_variants, phase queries,
# build_selected_phase_queries) lives in query_builders.py; re-imported at the top.








# URL utils (_normalized_domain/_normalize_url_for_compare/plausibility) live in
# url_utils.py; re-imported at the top of this module.


# Address parsing/matching (fragments, locality/city/postal, markers, alignment) lives
# in address.py; re-imported at the top of this module.










































# Google-Maps scoring/heuristic-confidence (_score_gmaps_candidates/_select_best_gmaps_website/
# _gmaps_confidence_*) lives in gmaps_scoring.py; re-imported at the top.










# is_disallowed_official_url/canonicalize_official_url/dedupe_candidate_urls live in
# url_utils.py; re-imported at the top of this module.


# Gemini LLM helpers (_parse_json_from_text, _gemini_generate_content_json,
# *_with_gemini, choose_final_website_with_gemini) live in gemini_llm.py; re-imported at top.




# Cost math lives in cost.py; re-imported at the top of this module.
















# _domain_from_url lives in url_utils.py; re-imported at the top of this module.


# Per-mode executors live in serpwow/modes/*; re-imported at the top of this module.






# _normalize_website_input lives in url_utils.py; re-imported at the top of this module.











def get_s3_client():
    global s3_client
    if s3_client is None:
        region = os.getenv("S3_REGION") or "ap-south-1"
        connect_timeout = _get_int_env("S3_CONNECT_TIMEOUT_SEC", 3)
        read_timeout = _get_int_env("S3_READ_TIMEOUT_SEC", 5)
        s3_retries = _get_int_env("S3_MAX_RETRIES", 3)
        s3_client = boto3.client(
            "s3",
            region_name=region,
            config=BotoConfig(
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                # "adaptive" adds AWS's client-side rate limiter, which is their documented answer to
                # S3 SlowDown (HTTP 503): it backs off proactively instead of just retrying harder.
                retries={"max_attempts": max(1, s3_retries), "mode": "adaptive"},
                max_pool_connections=100,
            ),
        )
    return s3_client


def _write_json_to_s3_sync(key: str, data: dict[str, Any]) -> None:
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET not configured")
    payload = json.dumps(data, indent=2, ensure_ascii=True).encode("utf-8")
    get_s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType="application/json; charset=utf-8",
    )


def _read_json_from_s3_sync(key: str) -> dict[str, Any]:
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET not configured")
    response = get_s3_client().get_object(Bucket=bucket, Key=key)
    body = response["Body"].read().decode("utf-8")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError(f"S3 object {key} is not a JSON object")
    return data


def _find_s3_upload_key_sync(upload_id: str, suffix: str) -> Optional[str]:
    """Find an S3 object key for an upload artifact, trying flat layout first then nested.

    Used as the S3 fallback in read_upload_artifact when local disk has been cleared.
    head_object for the flat key is O(1); the list fallback for nested is a rare cold path.
    """
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        return None
    client = get_s3_client()
    # New layout is <company>/<pipeline>/<upload_id>/<suffix>; company+pipeline are
    # unknown at read time, so match by suffix across the whole bucket.
    target_suffix = f"/{upload_id}/{suffix}"
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, MaxKeys=1000):
            for obj in page.get("Contents", []):
                key = str(obj.get("Key") or "")
                if key.endswith(target_suffix):
                    return key
    except Exception:
        pass
    return None


def _list_state_keys_from_s3_sync(limit: int) -> list[str]:
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        return []

    client = get_s3_client()
    # New layout has no shared top-level prefix; scan the whole bucket and select
    # state.json keys by suffix.
    prefix = ""
    continuation_token: Optional[str] = None
    keys_with_time: list[tuple[str, Any]] = []

    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        response = client.list_objects_v2(**kwargs)
        for obj in response.get("Contents", []):
            key = str(obj.get("Key") or "")
            if key.endswith("/state.json"):
                mtime = obj.get("LastModified")
                if mtime:
                    keys_with_time.append((key, mtime))
        if not response.get("IsTruncated"):
            break
        continuation_token = response.get("NextContinuationToken")
        if not continuation_token:
            break

    # Sort by last modified time descending so most recent uploads are first
    keys_with_time.sort(key=lambda x: x[1], reverse=True)
    return [k for k, _ in keys_with_time[:limit]]


def _list_local_state_files_sync(limit: int) -> list[Path]:
    flat = list(UPLOAD_BASE_DIR.glob("*/state.json"))
    nested = list(UPLOAD_BASE_DIR.glob("*/*/state.json"))
    state_files = sorted(flat + nested, key=lambda p: p.stat().st_mtime, reverse=True)
    return state_files[:limit]


_TERMINAL_STATUSES = frozenset({"completed", "completed_with_errors", "failed"})
# upload_id -> monotonic time of the last state.json S3 mirror (see write_upload_artifact).
_last_state_s3_flush: dict[str, float] = {}


def _is_terminal_state_status(data: dict[str, Any]) -> bool:
    return str(data.get("status") or "") in _TERMINAL_STATUSES


async def write_upload_artifact(upload_id: str, name: str, data: dict[str, Any]) -> None:
    # 1. Always write to the local filesystem first for immediate local consistency.
    #    Off the event loop: this is the hot path (once per row per update) and a large
    #    state.json blocks every other in-flight row's HTTP while it serializes.
    local_path = _state_file(upload_id) if name == "state" else _output_file(upload_id)
    await asyncio.to_thread(_write_json, local_path, data)

    # 2. If S3 is enabled, schedule S3 write in the background
    use_s3 = bool(os.getenv("S3_BUCKET"))
    if use_s3:
        company_name = str(data.get("company_name") or "")
        pipeline = str(data.get("pipeline") or "")
        key = (
            _state_s3_key(upload_id, company_name, pipeline)
            if name == "state"
            else _output_s3_key(upload_id, company_name, pipeline)
        )
        _remember_s3_run_prefix(upload_id, key.rsplit("/", 1)[0])

        # state.json is re-PUT to the SAME key on every row update, and it grows with the
        # run — a 100-row run uploaded ~84MB of near-identical copies (2 per row x 420KB),
        # which saturates uplink and hot-spots one S3 key (the documented SlowDown cause).
        # Throttle it: local disk stays the source of truth and is written every time, the
        # S3 copy lags at most SERPWOW_S3_STATE_FLUSH_SEC. Terminal snapshots ALWAYS
        # mirror, so the final state is never missing from S3 — and rows are idempotent,
        # so a cold-start resume from a slightly stale mirror only re-does safe work.
        if name == "state" and not _is_terminal_state_status(data):
            interval = _get_float_env("SERPWOW_S3_STATE_FLUSH_SEC", 5.0)
            now = time.monotonic()
            if (now - _last_state_s3_flush.get(upload_id, 0.0)) < interval:
                return
            _last_state_s3_flush[upload_id] = now
        elif name == "state":
            _last_state_s3_flush.pop(upload_id, None)   # terminal: stop tracking

        async def _write_s3_background():
            max_retries = 5
            base_delay = 1.0
            for attempt in range(1, max_retries + 1):
                try:
                    await asyncio.to_thread(_write_json_to_s3_sync, key, data)
                    return
                except Exception as exc:
                    if attempt == max_retries:
                        print(f"[s3_background_write] Permanent failure: Failed to upload state for {upload_id} to S3 after {max_retries} attempts: {type(exc).__name__}: {exc}")
                    else:
                        delay = base_delay * (2 ** (attempt - 1))
                        print(f"[s3_background_write] Attempt {attempt} failed for {upload_id}, retrying in {delay:.1f}s: {type(exc).__name__}: {exc}")
                        await asyncio.sleep(delay)
                
        asyncio.create_task(_write_s3_background())


async def read_upload_artifact(upload_id: str, name: str) -> dict[str, Any]:
    # 1. Try reading from the local filesystem first
    local_path = _state_file(upload_id) if name == "state" else _output_file(upload_id)
    if local_path.exists():
        try:
            data = json.loads(local_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if name == "state" and os.getenv("S3_BUCKET"):
                    run_prefix = _restore_s3_run_prefix(upload_id)
                    if run_prefix is None:
                        try:
                            state_key = await asyncio.to_thread(
                                _find_s3_upload_key_sync,
                                upload_id,
                                "state.json",
                            )
                        except Exception:
                            state_key = None
                        if state_key:
                            _remember_s3_run_prefix(
                                upload_id, state_key.rsplit("/", 1)[0])
                if name == "state":
                    tombstone = await _read_batch_deletion_tombstone(upload_id, data)
                    if tombstone:
                        _apply_batch_deletion_tombstone(data, tombstone)
                return data
        except Exception:
            pass

    # 2. If not found locally and S3 is enabled, download from S3
    use_s3 = bool(os.getenv("S3_BUCKET"))
    if use_s3:
        suffix = "state.json" if name == "state" else "output.json"
        key = await asyncio.to_thread(_find_s3_upload_key_sync, upload_id, suffix)
        if key is None:
            raise FileNotFoundError(f"upload {upload_id!r} not found in S3")
        data = await asyncio.to_thread(_read_json_from_s3_sync, key)
        _remember_s3_run_prefix(upload_id, key.rsplit("/", 1)[0])
        if name == "state":
            tombstone = await _read_batch_deletion_tombstone(upload_id, data)
            if tombstone:
                _apply_batch_deletion_tombstone(data, tombstone)
        # Cache it locally so subsequent reads are instant
        try:
            _write_json(local_path, data)
        except Exception:
            pass
        return data

    raise FileNotFoundError(str(local_path))


def _batch_deletion_tombstone_path(upload_id: str) -> Path:
    return _find_upload_dir(upload_id) / ".batch_deleted_by_user.json"


def _batch_deletion_tombstone_s3_key(upload_id: str, state: dict[str, Any]) -> str:
    prefix = _resolved_upload_s3_prefix(
        upload_id,
        str(state.get("company_name") or ""),
        str(state.get("pipeline") or ""),
    )
    return f"{prefix}/batch_deleted_by_user.json"


def _batch_deletion_tombstone_version(marker: dict[str, Any]) -> tuple[int, float, str, int]:
    """Return a sortable version for reconciling local and shared markers."""
    try:
        generation = max(0, int(marker.get("generation") or 0))
    except (TypeError, ValueError):
        generation = 0
    event_at = str(
        marker.get("cleared_at") or marker.get("deleted_by_user_at") or ""
    ).strip()
    parsed_at = _parse_iso_datetime(event_at)
    if parsed_at is not None:
        if parsed_at.tzinfo is None:
            parsed_at = parsed_at.replace(tzinfo=timezone.utc)
        timestamp = parsed_at.timestamp()
    else:
        timestamp = 0.0
    # A clear marker wins an exact tie so a retry cannot be reverted by a
    # duplicate deletion marker written for the same generation and instant.
    return generation, timestamp, event_at, int(bool(marker.get("cleared_at")))


async def _read_batch_deletion_tombstone(
    upload_id: str,
    state: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    local_path = _batch_deletion_tombstone_path(upload_id)
    local_data: Optional[dict[str, Any]] = None
    if local_path.exists():
        try:
            data = json.loads(local_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and (
                data.get("deleted_by_user_at") or data.get("cleared_at")
            ):
                local_data = data
        except Exception:
            pass
    if not os.getenv("S3_BUCKET") or not isinstance(state, dict):
        return local_data
    key = _batch_deletion_tombstone_s3_key(upload_id, state)
    try:
        data = await asyncio.to_thread(_read_json_from_s3_sync, key)
    except Exception:
        return local_data
    if not isinstance(data, dict) or not (
        data.get("deleted_by_user_at") or data.get("cleared_at")
    ):
        return local_data
    selected = max(
        (marker for marker in (local_data, data) if isinstance(marker, dict)),
        key=_batch_deletion_tombstone_version,
    )
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(local_path, selected)
    except Exception:
        pass
    return selected


def _apply_batch_deletion_tombstone(
    state: dict[str, Any],
    tombstone: dict[str, Any],
) -> None:
    batch = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
    state_generation = _batch_generation(state)
    try:
        artifact_generation = max(0, int(tombstone.get("generation") or 0))
    except (TypeError, ValueError):
        artifact_generation = 0
    if artifact_generation < state_generation:
        return
    batch["generation"] = max(state_generation, artifact_generation)
    state["gemini_batch"] = batch
    if tombstone.get("cleared_at"):
        if state_generation < artifact_generation:
            batch = {
                "status": "waiting_for_rows",
                "generation": artifact_generation,
                "queued_at": None,
                "job_name": None,
                "error": None,
            }
            state["gemini_batch"] = batch
        state.pop("batch_deleted_by_user_at", None)
        state.pop("batch_deleted_job_names", None)
        batch.pop("deleted_by_user_at", None)
        batch.pop("deleted_jobs_by_user_at", None)
        return
    deleted_at = str(tombstone.get("deleted_by_user_at") or "").strip()
    if not deleted_at:
        return
    deleted_jobs = {
        str(job_name).strip()
        for job_name in (tombstone.get("job_names") or [])
        if str(job_name).strip()
    }
    deletion_error = "Remote batch deleted by user"
    top_level_deleted = bool(tombstone.get("top_level_deleted"))
    if str(batch.get("job_name") or "").strip() in deleted_jobs:
        batch.pop("job_name", None)
    chunks = batch.get("chunks") if isinstance(batch.get("chunks"), list) else []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        if str(chunk.get("job_name") or "").strip() not in deleted_jobs:
            continue
        chunk.pop("job_name", None)
        chunk["status"] = "failed"
        chunk["error"] = deletion_error
    state["batch_deleted_job_names"] = sorted(deleted_jobs)
    if top_level_deleted:
        batch["status"] = "failed"
        batch["completed_at"] = deleted_at
        batch["deleted_by_user_at"] = deleted_at
        batch["error"] = deletion_error
        state["batch_deleted_by_user_at"] = deleted_at
    else:
        batch["status"] = _aggregate_batch_chunk_status(chunks)
        batch["deleted_jobs_by_user_at"] = deleted_at
        if batch["status"] in {"waiting_for_rows", "queued", "running", "cancel_requested"}:
            batch.pop("completed_at", None)
            batch["error"] = None
    state["gemini_batch"] = batch


def _aggregate_batch_chunk_status(chunks: list[Any]) -> str:
    statuses = [
        str(chunk.get("status") or "")
        for chunk in chunks
        if isinstance(chunk, dict)
    ]
    if not statuses or any(status in {
        "waiting_for_rows", "queued", "running", "cancel_requested", "",
    } for status in statuses):
        return "running"
    if all(status == "succeeded" for status in statuses):
        return "succeeded"
    if all(status in {"failed", "cancelled", "skipped"} for status in statuses):
        return "failed"
    return "completed_with_errors"


async def _write_batch_deletion_tombstone(
    upload_id: str,
    state: dict[str, Any],
    job_name: str,
    deleted_at: str,
    *,
    top_level_deleted: bool,
) -> dict[str, Any]:
    existing = await _read_batch_deletion_tombstone(upload_id, state) or {}
    if existing.get("cleared_at"):
        existing = {}
    job_names = {
        str(value).strip()
        for value in (existing.get("job_names") or [])
        if str(value).strip()
    }
    job_names.add(job_name)
    tombstone = {
        "upload_id": upload_id,
        "deleted_by_user_at": str(existing.get("deleted_by_user_at") or deleted_at),
        "job_names": sorted(job_names),
        "top_level_deleted": bool(existing.get("top_level_deleted")) or top_level_deleted,
        "generation": max(
            _batch_generation(state),
            int(existing.get("generation") or 0),
        ),
    }
    local_path = _batch_deletion_tombstone_path(upload_id)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(local_path, tombstone)
    if os.getenv("S3_BUCKET"):
        key = _batch_deletion_tombstone_s3_key(upload_id, state)
        await asyncio.to_thread(_write_json_to_s3_sync, key, tombstone)
    return tombstone


async def _clear_batch_deletion_tombstone(
    upload_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    existing = await _read_batch_deletion_tombstone(upload_id, state) or {}
    try:
        artifact_generation = max(0, int(existing.get("generation") or 0))
    except (TypeError, ValueError):
        artifact_generation = 0
    clear_marker = {
        "upload_id": upload_id,
        "cleared_at": _now_iso(),
        "deleted_by_user_at": None,
        "job_names": [],
        "top_level_deleted": False,
        "generation": max(_batch_generation(state), artifact_generation) + 1,
    }
    local_path = _batch_deletion_tombstone_path(upload_id)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(local_path, clear_marker)
    if os.getenv("S3_BUCKET"):
        key = _batch_deletion_tombstone_s3_key(upload_id, state)
        await asyncio.to_thread(_write_json_to_s3_sync, key, clear_marker)
    return clear_marker


def _write_text_to_s3_sync(key: str, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET not configured")
    get_s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=text.encode("utf-8"),
        ContentType=content_type,
    )


async def write_upload_text_artifact(upload_id: str, name: str, text: str, content_type: str, company_name: str = "", pipeline: str = "") -> None:
    use_s3 = bool(os.getenv("S3_BUCKET"))
    if use_s3:
        if name == "batch_input_jsonl":
            key = _batch_input_jsonl_s3_key(upload_id, company_name, pipeline)
        elif name == "batch_output_json":
            key = _batch_output_json_s3_key(upload_id, company_name, pipeline)
        else:
            raise ValueError(f"Unsupported text artifact: {name}")
        await asyncio.to_thread(_write_text_to_s3_sync, key, text, content_type)
        return

    if name == "batch_input_jsonl":
        local_path = _find_upload_dir(upload_id) / "gemini_batch_input.jsonl"
    elif name == "batch_output_json":
        local_path = _find_upload_dir(upload_id) / "gemini_batch_output.json"
    else:
        raise ValueError(f"Unsupported text artifact: {name}")
    local_path.write_text(text, encoding="utf-8")


# Per-row raw-response artifact naming, by pipeline. The folder and filename suffix say
# WHICH PROVIDER produced the bytes, so a stored object is self-describing — a
# firmographics row holds a scrape.do SERP, not a SerpWow response. gsearch is the only
# pipeline still on SerpWow and keeps its existing names, so its old and new runs stay in
# one folder.
_RAW_ARTIFACT_NAMES: dict[str, tuple[str, str]] = {
    PIPELINE_FIRMOGRAPHICS: ("search_response", "search"),
}
_DEFAULT_RAW_ARTIFACT_NAMES = ("serpwow_response", "serpwow")


def _raw_artifact_names(pipeline: str) -> tuple[str, str]:
    """(folder, filename suffix) for one pipeline's per-row raw provider response."""
    return _RAW_ARTIFACT_NAMES.get(str(pipeline or ""), _DEFAULT_RAW_ARTIFACT_NAMES)


def _upload_raw_response_sync(upload_id: str, row_index: int, raw_json: str, pipeline: str = "", upload_company_name: str = "", row_company_name: str = "") -> str:
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET not configured")

    # Folder uses the UPLOAD company (one folder per run); the per-row filename
    # uses the ROW company so each response is identifiable. Per-row raw responses
    # live in a provider-named subfolder, apart from the run aggregates.
    safe_name = _safe_name(row_company_name or upload_company_name)
    prefix = _resolved_upload_s3_prefix(
        upload_id, upload_company_name, pipeline)
    folder, suffix = _raw_artifact_names(pipeline)
    key = f"{prefix}/{folder}/{row_index:06d}_{safe_name}_{suffix}.json"
    get_s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=raw_json.encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    return key


async def upload_raw_response_to_s3(upload_id: str, row_index: int, raw_json: str, pipeline: str = "", upload_company_name: str = "", row_company_name: str = "") -> tuple[Optional[str], Optional[str]]:
    if not raw_json:
        return None, "No raw provider response available to upload"
    try:
        key = await asyncio.to_thread(
            _upload_raw_response_sync,
            upload_id,
            row_index,
            raw_json,
            pipeline,
            upload_company_name,
            row_company_name,
        )
        return key, None
    except Exception as exc:
        return None, str(exc)


def _raw_response_key(row: dict[str, Any]) -> Optional[str]:
    """The row's raw-provider-response S3 key.

    Reads the current field, then the two legacy names. Both legacy keys always held the
    SAME value -- ``s3_html_key`` never contained HTML, and ``s3_serpwow_json_key`` was
    SerpWow-named while carrying a scrape.do payload for every migrated pipeline -- so they
    collapsed into one honestly-named field on 2026-08-19. The fallback is what keeps runs
    written before that readable.
    """
    for key in ("raw_response_s3_key", "s3_serpwow_json_key", "s3_html_key"):
        value = row.get(key)
        if value:
            return str(value)
    return None


def build_upload_output_payload(state: dict[str, Any]) -> dict[str, Any]:
    timing_summary = build_processing_timing_summary(state.get("rows", []))
    # Total = wall-clock (start→completion); avg/count = per-row work stats. Mirror
    # summarize_upload_state so the output payload agrees with /status and the cache.
    elapsed = _run_elapsed_seconds(state)
    total_seconds = elapsed if elapsed is not None else timing_summary["processing_seconds_total"]
    return {
        "upload_id": state["upload_id"],
        "company_name": state.get("company_name") or "",
        "pipeline": state.get("pipeline") or "",
        "status": state["status"],
        "gemini_batch": state.get("gemini_batch"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "total_rows": state["total_rows"],
        "processed_rows": state["processed_rows"],
        "success_rows": state["success_rows"],
        "failed_rows": state["failed_rows"],
        "processing_seconds_total": total_seconds,
        "processing_seconds_avg": timing_summary["processing_seconds_avg"],
        "processing_seconds_count": timing_summary["processing_seconds_count"],
        "results": [
            {
                "row_index": row["row_index"],
                "input": {
                    "company_name": row["company_name"],
                    "country": row["country"],
                    "firm_id": row.get("firm_id"),
                    "industry": row.get("industry"),
                    "full_address": row.get("full_address"),
                    "official_website": row.get("official_website"),
                },
                "status": row["status"],
                "error": row.get("error"),
                "raw_response_s3_key": _raw_response_key(row),
                "output": row.get("result"),
            }
            for row in state.get("rows", [])
        ],
    }


# XLSX export (_sanitize_excel_text/_excel_col_name/_xlsx_cell_xml/
# build_upload_output_xlsx_bytes) lives in output_export.py; re-imported at the top.


def _build_batch_prompt_for_row(row: dict[str, Any]) -> str:
    ctx_probe = ((row.get("result") or {}).get("context")
                 if isinstance((row.get("result") or {}).get("context"), dict) else {})
    if ctx_probe.get("pipeline") == PIPELINE_FIRMOGRAPHICS:
        # A different question entirely: normalise this row's AI Overview into the six
        # firmographic fields. Byte-identical to the inline prompt (one builder in
        # gemini_llm), so a batched run cannot answer a row differently from an inline one.
        scrapedo = ctx_probe.get("scrapedo") if isinstance(ctx_probe.get("scrapedo"), dict) else {}
        overview = scrapedo.get("ai_overview")
        return build_ai_overview_prompt(
            str(row.get("company_name") or ""),
            str(row.get("country") or ""),
            str((row.get("result") or {}).get("official_website") or ""),
            overview if isinstance(overview, dict) else {},
        )
    input_obj = {
        "company_name": row.get("company_name"),
        "country": row.get("country"),
        "firm_id": row.get("firm_id"),
        "industry": row.get("industry"),
        "full_address": row.get("full_address"),
    }
    result_obj = row.get("result") if isinstance(row.get("result"), dict) else {}
    context_obj = result_obj.get("context") if isinstance(result_obj.get("context"), dict) else {}
    search_attempts = context_obj.get("search_attempts") if isinstance(context_obj.get("search_attempts"), list) else []
    serpwow_obj = context_obj.get("serpwow") if isinstance(context_obj.get("serpwow"), dict) else {}
    gmaps_obj = context_obj.get("gmaps") if isinstance(context_obj.get("gmaps"), dict) else {}
    ai_search_obj = context_obj.get("ai") if isinstance(context_obj.get("ai"), dict) else {}
    current_official = result_obj.get("official_website")
    candidate_urls: list[str] = []
    gmaps_official = gmaps_obj.get("official_website")
    if isinstance(gmaps_official, str) and gmaps_official.strip():
        candidate_urls.append(gmaps_official.strip())
    if isinstance(current_official, str) and current_official.strip():
        candidate_urls.append(current_official.strip())
    for attempt in search_attempts:
        if not isinstance(attempt, dict):
            continue
        value = attempt.get("official_website")
        if isinstance(value, str) and value.strip():
            candidate_urls.append(value.strip())
    # Also include the full pre-deduped candidate list written by the gsearch
    # worker (context["candidates"]).  For the full pipeline this key is absent,
    # so the loop below is a no-op for that path.
    for cand in (context_obj.get("candidates") or []):
        if isinstance(cand, str) and cand.strip():
            candidate_urls.append(cand.strip())
    dedup: list[str] = []
    seen: set[str] = set()
    for url in candidate_urls:
        if url and url not in seen and not is_disallowed_official_url(url):
            seen.add(url)
            dedup.append(url)

    prompt = (
        "You are a company website resolver and profile extractor.\n"
        "Use the provided search evidence and return strict JSON only.\n"
        "Schema:\n"
        "{\n"
        '  "official_website": string|null,\n'
        '  "confidence_score": number,\n'
        '  "confidence": "high"|"medium"|"low",\n'
        '  "summary": string,\n'
        '  "website_company_descirption_ai": string,\n'
        '  "website_company_descirption_translated_ai": string,\n'
        '  "reason": string,\n'
        '  "evidence": [string],\n'
        '  "address": string|null,\n'
        '  "phone": string|null,\n'
        '  "email": string|null,\n'
        '  "industry": string|null,\n'
        '  "products": [string],\n'
        '  "services": [string],\n'
        '  "alternatives": [string]\n'
        "}\n"
        "Rules:\n"
        "- official_website must be the most likely official company site URL.\n"
        "- Never return directory/listing/social/wiki/search/file URLs.\n"
        "- If uncertain set official_website to null.\n"
        "- confidence_score is 0-100 and confidence should align with it.\n"
        "- For address/phone/email/industry/products/services use only provided evidence.\n"
        "- If unavailable return null for scalar fields and [] for lists.\n"
        "- website_company_descirption_ai must be in the company's country language when available.\n"
        "- website_company_descirption_ai must be plain text with no links, citations, or meta remarks.\n"
        "- If meaningful information is available, website_company_descirption_ai must be at least 250 characters.\n"
        "- If no meaningful public information is available, set website_company_descirption_ai to '-'.\n"
        "- website_company_descirption_translated_ai must be formal, neutral, English corporate profile text.\n"
        "- website_company_descirption_translated_ai must be plain text with no links, citations, or meta remarks.\n"
        "- website_company_descirption_translated_ai must use ASCII only.\n"
        "- If meaningful information is available, website_company_descirption_translated_ai must be at least 250 characters.\n"
        "- If no meaningful public information is available, set website_company_descirption_translated_ai to '-'.\n\n"
        f"Input: {json.dumps(input_obj, ensure_ascii=True)}\n\n"
        f"Candidate URLs: {json.dumps(dedup, ensure_ascii=True)}\n\n"
        f"Search Attempts: {json.dumps(search_attempts, ensure_ascii=True)[:6000]}\n\n"
        f"SerpWow Context: {json.dumps(serpwow_obj, ensure_ascii=True)[:8000]}\n\n"
        f"GMaps Context: {json.dumps(gmaps_obj, ensure_ascii=True)[:6000]}\n\n"
        f"Search AI Context: {json.dumps(ai_search_obj, ensure_ascii=True)[:4000]}"
    )
    return prompt


def _gemini_batch_create_sync(
    model: str,
    requests_payload: list[dict[str, Any]],
    upload_id: Optional[str] = None,
) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:batchGenerateContent?key={api_key}"
    )
    display_name = f"single-ra-{uuid.uuid4()}"
    safe_upload_id = str(upload_id or "").strip()
    if safe_upload_id:
        display_name = f"single-ra-upload-{safe_upload_id}-{uuid.uuid4()}"
    body = {
        "batch": {
            "display_name": display_name,
            "input_config": {
                "requests": {
                    "requests": requests_payload,
                }
            },
        }
    }
    req = Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=60) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def _gemini_batch_get_sync(batch_name: str) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/{batch_name}?key={api_key}"
    req = Request(endpoint, headers={"Content-Type": "application/json"}, method="GET")
    with urlopen(req, timeout=45) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def _gemini_batch_cancel_sync(batch_name: str) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/{batch_name}:cancel?key={api_key}"
    req = Request(
        endpoint,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=45) as response:
        raw = response.read().decode("utf-8")
    if not raw.strip():
        return {}
    return json.loads(raw)


def _gemini_batch_delete_sync(batch_name: str) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/{batch_name}?key={api_key}"
    req = Request(
        endpoint,
        headers={"Content-Type": "application/json"},
        method="DELETE",
    )
    with urlopen(req, timeout=45) as response:
        raw = response.read().decode("utf-8")
    if not raw.strip():
        return {}
    return json.loads(raw)


def _gemini_batch_list_sync(limit: int = 200, timeout_sec: float = 10.0) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")

    ops: list[dict[str, Any]] = []
    page_token = ""
    remaining = max(1, int(limit))
    while remaining > 0:
        page_size = min(100, remaining)
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/batches"
            f"?key={api_key}&pageSize={page_size}"
        )
        if page_token:
            endpoint += f"&pageToken={quote(page_token, safe='')}"
        req = Request(endpoint, headers={"Content-Type": "application/json"}, method="GET")
        with urlopen(req, timeout=max(1.0, float(timeout_sec))) as response:
            raw = response.read().decode("utf-8")
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            break
        current_ops = payload.get("operations")
        # Some API revisions may return `batches` directly.
        if not isinstance(current_ops, list):
            current_ops = payload.get("batches")
        if isinstance(current_ops, list):
            for op in current_ops:
                if isinstance(op, dict):
                    ops.append(op)
                    remaining -= 1
                    if remaining <= 0:
                        break
        next_token = payload.get("nextPageToken")
        if not next_token or remaining <= 0:
            break
        page_token = str(next_token)
    return {"operations": ops}


def _extract_text_from_generate_response(resp_obj: dict[str, Any]) -> str:
    if not isinstance(resp_obj, dict):
        return ""
    candidates = resp_obj.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    first = candidates[0] if isinstance(candidates[0], dict) else {}
    content = first.get("content") if isinstance(first, dict) else {}
    parts = content.get("parts") if isinstance(content, dict) else []
    if not isinstance(parts, list) or not parts:
        return ""
    first_part = parts[0] if isinstance(parts[0], dict) else {}
    return str(first_part.get("text") or "")


def _gemini_batch_state_name(batch_obj: dict[str, Any]) -> str:
    if not isinstance(batch_obj, dict):
        return ""

    # Old shape: {"state": {"name": "JOB_STATE_SUCCEEDED"}}
    state_obj = batch_obj.get("state")
    if isinstance(state_obj, dict):
        value = str(state_obj.get("name") or "").strip()
        if value:
            return value
    elif isinstance(state_obj, str) and state_obj.strip():
        return state_obj.strip()

    # New shape (LRO): {"done": true, "metadata": {"state": "BATCH_STATE_SUCCEEDED"}}
    metadata = batch_obj.get("metadata")
    if isinstance(metadata, dict):
        metadata_state = metadata.get("state")
        if isinstance(metadata_state, str) and metadata_state.strip():
            return metadata_state.strip()

    if bool(batch_obj.get("done")):
        return "DONE"
    return ""


def _gemini_batch_is_terminal(state_name: str, done_flag: bool) -> bool:
    terminal_states = {
        "JOB_STATE_SUCCEEDED",
        "JOB_STATE_FAILED",
        "JOB_STATE_CANCELLED",
        "JOB_STATE_EXPIRED",
        "BATCH_STATE_SUCCEEDED",
        "BATCH_STATE_FAILED",
        "BATCH_STATE_CANCELLED",
        "BATCH_STATE_EXPIRED",
    }
    if state_name in terminal_states:
        return True
    return bool(done_flag)


def _gemini_batch_is_success(state_name: str, done_flag: bool, batch_obj: dict[str, Any]) -> bool:
    success_states = {"JOB_STATE_SUCCEEDED", "BATCH_STATE_SUCCEEDED"}
    if state_name in success_states:
        return True
    if not done_flag:
        return False
    error_obj = batch_obj.get("error")
    return not bool(error_obj)


def _extract_upload_id_from_batch_obj(batch_obj: dict[str, Any]) -> Optional[str]:
    if not isinstance(batch_obj, dict):
        return None
    metadata = batch_obj.get("metadata") if isinstance(batch_obj.get("metadata"), dict) else {}
    display_name = str(metadata.get("displayName") or metadata.get("display_name") or "").strip()
    if not display_name:
        return None
    marker = "single-ra-upload-"
    if display_name.startswith(marker):
        tail = display_name[len(marker):]
        # The generated suffix is a canonical UUID. Match the complete suffix
        # so every hyphen belonging to the upload ID remains intact.
        match = re.fullmatch(
            r"(?P<upload_id>.+)-[0-9a-f]{8}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            tail,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group("upload_id").strip() or None
    versioned_chunk_match = re.fullmatch(
        r"gsearch-(.+)-gen\d+-chunk\d+", display_name)
    if versioned_chunk_match:
        return versioned_chunk_match.group(1).strip() or None
    chunk_match = re.fullmatch(r"gsearch-(.+)-chunk\d+", display_name)
    if chunk_match:
        return chunk_match.group(1).strip() or None
    return None


def _extract_generation_from_batch_obj(batch_obj: dict[str, Any]) -> Optional[int]:
    if not isinstance(batch_obj, dict):
        return None
    metadata = batch_obj.get("metadata") if isinstance(batch_obj.get("metadata"), dict) else {}
    display_name = str(
        metadata.get("displayName") or metadata.get("display_name") or ""
    ).strip()
    match = re.fullmatch(r"gsearch-.+-gen(\d+)-chunk\d+", display_name)
    return int(match.group(1)) if match else None


def _derive_ui_batch_status(
    *,
    live_state: str,
    done_flag: bool,
    error_obj: Any,
    local_status: Optional[str],
) -> str:
    state = str(live_state or "").strip().upper()
    local = str(local_status or "").strip().lower()

    if state.endswith("SUCCEEDED"):
        return "succeeded"
    if state.endswith("FAILED") or state.endswith("EXPIRED"):
        return "failed"
    if state.endswith("CANCELLED"):
        return "cancelled"
    if done_flag:
        return "failed" if error_obj else "succeeded"

    if local in {
        "waiting_for_rows",
        "queued",
        "running",
        "cancel_requested",
        "cancelled",
        "succeeded",
        "completed_with_errors",
        "failed",
        "skipped",
    }:
        return local
    return "running"


def _extract_batch_inlined_responses(batch_obj: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(batch_obj, dict):
        return []

    # Old shape in app code.
    dest = batch_obj.get("dest")
    if isinstance(dest, dict):
        for key in ("inlinedResponses", "inlined_responses"):
            value = dest.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    # New LRO shape: response.inlinedResponses.inlinedResponses
    response_obj = batch_obj.get("response")
    if isinstance(response_obj, dict):
        nested = response_obj.get("inlinedResponses")
        if isinstance(nested, dict):
            value = nested.get("inlinedResponses")
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]

    # Some responses place it under metadata.output.inlinedResponses.inlinedResponses
    metadata = batch_obj.get("metadata")
    if isinstance(metadata, dict):
        output = metadata.get("output")
        if isinstance(output, dict):
            nested = output.get("inlinedResponses")
            if isinstance(nested, dict):
                value = nested.get("inlinedResponses")
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]

    return []


def _extract_batch_response_key(inline_item: dict[str, Any]) -> Optional[str]:
    if not isinstance(inline_item, dict):
        return None

    # Most likely shape: {"key": "row-12", "response": {...}}
    direct_key = inline_item.get("key")
    if isinstance(direct_key, str) and direct_key.strip():
        return direct_key.strip()

    # Alternate shape: {"metadata": {"key": "row-12"}, "response": {...}}
    metadata = inline_item.get("metadata")
    if isinstance(metadata, dict):
        meta_key = metadata.get("key")
        if isinstance(meta_key, str) and meta_key.strip():
            return meta_key.strip()

    # Some wrappers include request metadata echoed back.
    request_obj = inline_item.get("request")
    if isinstance(request_obj, dict):
        req_meta = request_obj.get("metadata")
        if isinstance(req_meta, dict):
            req_key = req_meta.get("key")
            if isinstance(req_key, str) and req_key.strip():
                return req_key.strip()

    return None


def _log_gemini_batch(upload_id: str, message: str) -> None:
    print(f"[gemini-batch][{_now_iso()}][upload:{upload_id}] {message}")


def _build_batch_items_for_state(state: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, int]]:
    """File-API shape: list of (key, gemini_request_dict) for every terminal row, plus
    a key->row_index map. Reuses _build_batch_prompt_for_row (folds in candidates)."""
    items: list[tuple[str, dict[str, Any]]] = []
    row_index_by_key: dict[str, int] = {}
    for row in state.get("rows", []):
        if not isinstance(row, dict) or row.get("status") not in {"completed", "failed"}:
            continue
        ctx = ((row.get("result") or {}).get("context")
               if isinstance((row.get("result") or {}).get("context"), dict) else {})
        if ctx.get("skip_llm"):
            # Worker-decided short-circuit rows were already finalized.
            continue
        if ctx.get("pipeline") == PIPELINE_FIRMOGRAPHICS:
            # No AI overview means there is nothing to normalise; a request would be paid
            # for and come back empty. The row is already terminal as not_found.
            _sd = ctx.get("scrapedo") if isinstance(ctx.get("scrapedo"), dict) else {}
            if not isinstance(_sd.get("ai_overview"), dict) or not _sd.get("ai_overview"):
                continue
        if ctx.get("pipeline") == PIPELINE_GSEARCH and not ctx.get("candidates"):
            # Defense for legacy/in-flight rows created before gsearch persisted
            # skip_llm: Gemini cannot select a URL from an empty candidate set.
            continue
        row_index = int(row.get("row_index", 0) or 0)
        key = f"row-{row_index}"
        prompt = _build_batch_prompt_for_row(row)
        request = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        items.append((key, request))
        row_index_by_key[key] = row_index
    return items, row_index_by_key


def _apply_batch_parsed_to_row(row: dict[str, Any], parsed: dict[str, Any],
                               row_usage: dict[str, Any], batch_model: str) -> str:
    """Apply one parsed Gemini result onto a row. Always returns 'completed' now:
    a batch-decided no-website row is a business not_found (outcome=not_found), not
    an error. Mirrors the existing single-job mapping (candidate-set guard via
    is_disallowed_official_url; domain-mismatch is non-fatal -> flag, keep URL)."""
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    context = result.get("context") if isinstance(result.get("context"), dict) else {}
    row_batch_cost_usd = calculate_gemini_batch_cost_usd(row_usage or {})
    prev_gemini = _as_float(result.get("gemini_cost_usd"), 0.0)
    prev_total = _as_float(result.get("total_cost_usd"), 0.0)
    updated_gemini = round(prev_gemini + row_batch_cost_usd, 8)
    updated_total = round(prev_total + row_batch_cost_usd, 8)
    selected_url = parsed.get("official_website")
    if isinstance(selected_url, str) and selected_url.strip() and not is_disallowed_official_url(selected_url):
        result["official_website"] = selected_url.strip()
    for k in ("address", "phone", "email", "industry",
              "website_company_descirption_ai", "website_company_descirption_translated_ai"):
        if parsed.get(k) is not None:
            result[k] = parsed.get(k)
    result["summary"] = str(parsed.get("summary") or result.get("summary") or "")
    for k in ("products", "services"):
        if isinstance(parsed.get(k), list):
            result[k] = parsed.get(k)
    result["gemini_cost_usd"] = updated_gemini
    result["total_cost_usd"] = updated_total
    cb = context.get("cost_breakdown") if isinstance(context.get("cost_breakdown"), dict) else {}
    cb["gemini_batch_cost_usd"] = round(_as_float(cb.get("gemini_batch_cost_usd"), 0.0) + row_batch_cost_usd, 8)
    cb["gemini_cost_usd"] = updated_gemini
    cb["total_cost_usd"] = updated_total
    context["cost_breakdown"] = cb
    context["gemini_batch_ai"] = {"provider": "google-gemini-batch", "model": batch_model,
                                  "used": True, "usage": row_usage, "cost_usd": row_batch_cost_usd,
                                  "raw": parsed, "error": None}
    result["context"] = context
    row["result"] = result
    if context.get("pipeline") == PIPELINE_FIRMOGRAPHICS:
        # official_website is this pipeline's INPUT, so it cannot decide the outcome (D43).
        # What the batch produced decides it: fields extracted -> found, nothing -> a
        # business not_found.
        row["status"] = "completed"
        row["error_source"] = None
        row["error_category"] = None
        if reporting.row_produced_a_result(result, PIPELINE_FIRMOGRAPHICS):
            row["error"] = None
            row["outcome"] = _outcomes.OUTCOME_FOUND
        else:
            row["error"] = _outcomes.BATCH_NOT_FOUND
            row["outcome"] = _outcomes.OUTCOME_NOT_FOUND
        return "completed"
    finalized = str(result.get("official_website") or "").strip()
    if finalized:
        if not _official_website_looks_plausible(finalized, str(row.get("company_name") or ""),
                                                 str(row.get("country") or "")):
            parsed["domain_name_mismatch"] = True
        row["status"] = "completed"
        row["error"] = None
        row["outcome"] = _outcomes.OUTCOME_FOUND
        row["error_source"] = None
        row["error_category"] = None
        return "completed"
    row["status"] = "completed"
    row["error"] = _outcomes.BATCH_NOT_FOUND
    row["outcome"] = _outcomes.OUTCOME_NOT_FOUND
    row["error_source"] = None
    row["error_category"] = None
    return "completed"


async def _persist_chunk_meta(
    upload_id: str,
    chunk_id: int,
    patch: dict[str, Any],
    expected_generation: int,
) -> None:
    async with get_upload_lock(upload_id):
        state = await read_upload_artifact(upload_id, "state")
        if _batch_deleted_by_user(state):
            return
        if _batch_generation(state) != expected_generation:
            return
        deleted_jobs = {
            str(value).strip()
            for value in (state.get("batch_deleted_job_names") or [])
            if str(value).strip()
        }
        if str(patch.get("job_name") or "").strip() in deleted_jobs:
            return
        gb = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
        chunks = gb.get("chunks") if isinstance(gb.get("chunks"), list) else []
        found = next((c for c in chunks if isinstance(c, dict) and c.get("chunk_id") == chunk_id), None)
        if found is None:
            found = {"chunk_id": chunk_id}; chunks.append(found)
        found.update(patch)
        gb["chunks"] = chunks
        state["gemini_batch"] = gb
        await persist_upload_state(upload_id, state)


async def _run_one_gemini_chunk(upload_id: str, chunk_id: int,
                                items: list[tuple[str, dict[str, Any]]],
                                existing_job_name: str, batch_model: str,
                                driver_generation: int = 0) -> dict[str, Any]:
    """Submit (or resume) one chunk's Gemini batch job, poll to terminal, and return
    {chunk_id, job_name, status, error, parsed_by_row, usage}. Never raises — a failed
    chunk returns status='failed' with its rows unmapped (-> not found)."""
    from app.services.ai_mode import gemini_batch as gb
    poll_interval = llm_batch.poll_sec()
    poll_timeout = llm_batch.timeout_sec()
    result = {"chunk_id": chunk_id, "job_name": existing_job_name or None,
              "status": "failed", "error": None, "parsed_by_row": {}, "usage": {}}
    try:
        job_name = existing_job_name
        create_obj: dict[str, Any] = {}
        if not job_name:
            create_obj = await asyncio.to_thread(
                lambda: gb.create_batch(
                    batch_model,
                    items,
                    display_name=(
                        f"gsearch-{upload_id}-gen{driver_generation}-chunk{chunk_id}"
                    ),
                ))
            job_name = gb.batch_name_from_create(create_obj)
            if not job_name:
                raise RuntimeError(f"no job name from create: {create_obj}")
        result["job_name"] = job_name
        _log_gemini_batch(upload_id, f"chunk={chunk_id} submitted job_name={job_name} rows={len(items)}")
        # Persist the job_name immediately so a restart can re-poll this chunk.
        try:
            await _persist_chunk_meta(
                upload_id,
                chunk_id,
                {"job_name": job_name, "status": "running"},
                driver_generation,
            )
        except Exception as _pm_exc:
            _log_gemini_batch(upload_id, f"chunk={chunk_id} _persist_chunk_meta failed (non-fatal): {_pm_exc!r}")
        deadline = asyncio.get_event_loop().time() + poll_timeout
        final_obj: dict[str, Any] = {}
        while True:
            try:
                obj = await asyncio.to_thread(gb.get_batch, job_name)
            except Exception as exc:
                if asyncio.get_event_loop().time() >= deadline:
                    raise TimeoutError(f"chunk {chunk_id} poll timeout: {exc}") from exc
                await asyncio.sleep(min(poll_interval, 5)); continue
            sname = gb.state_name(obj); done = bool(obj.get("done"))
            if gb.is_terminal(sname, done):
                # Merge create_obj's extra fields (e.g. _keys in tests) into the terminal object
                # so collect_results has access to any metadata stored at create time.
                final_obj = {**create_obj, **obj}; break
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(f"chunk {chunk_id} batch timeout after {poll_timeout}s")
            await asyncio.sleep(poll_interval)
        sname = gb.state_name(final_obj); done = bool(final_obj.get("done"))
        # The private _failed_states set that used to sit here is now inside
        # gb.is_success (gemini_batch.FAILED_STATES) — same rule, one copy, and the two
        # S3-only runners get the defence this driver had all along.
        if not gb.is_success(sname, done, final_obj):
            raise RuntimeError(f"chunk {chunk_id} ended state={sname} error={final_obj.get('error')}")
        records = await asyncio.to_thread(gb.collect_results, final_obj)
        row_index_by_key = {k: int(k.split("-")[1]) for k, _ in items}
        parsed_by_row, usage_by_row = {}, {}
        for rec in records:
            ridx = row_index_by_key.get(str(rec.get("key") or ""))
            if ridx is None:
                continue
            parsed_by_row[ridx] = gb.parse_json_from_text(rec.get("text") or "") or {}
            usage_by_row[ridx] = rec.get("usage") or {}
        result.update(status="succeeded", parsed_by_row=parsed_by_row, usage=usage_by_row)
        _log_gemini_batch(upload_id, f"chunk={chunk_id} succeeded job_name={job_name} mapped={len(parsed_by_row)}")
    except Exception as exc:
        result["status"] = "failed"; result["error"] = str(exc)
        _log_gemini_batch(upload_id, f"chunk={chunk_id} FAILED job_name={result['job_name']} error={repr(exc)}")
    return result


async def run_gemini_batch_for_upload(upload_id: str) -> None:
    driver_generation = 0
    try:
        async with get_upload_lock(upload_id):
            state = await read_upload_artifact(upload_id, "state")
            if _batch_deleted_by_user(state):
                return
            gb = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
            entry_status = str(gb.get("status") or "")
            legacy_waiting = entry_status == "waiting_for_rows" and _batch_generation(state) == 0
            if entry_status not in {"queued", "running"} and not legacy_waiting:
                return
            driver_generation = _batch_generation(state)
            gb_started = gb.get("started_at") or _now_iso()
            existing_chunks = gb.get("chunks") if isinstance(gb.get("chunks"), list) else []
            state["gemini_batch"] = {**gb, "status": "running", "started_at": gb_started,
                                     "chunks": existing_chunks, "error": None,
                                     "generation": driver_generation}
            await persist_upload_state(upload_id, state)

        items, row_index_by_key = _build_batch_items_for_state(state)
        if not items:
            async with get_upload_lock(upload_id):
                state = await read_upload_artifact(upload_id, "state")
                if (_batch_deleted_by_user(state)
                        or _batch_generation(state) != driver_generation):
                    return
                state["gemini_batch"] = {**state.get("gemini_batch", {}), "status": "skipped",
                                         "completed_at": _now_iso(),
                                         "error": "No rows available for Gemini batch processing."}
                await persist_upload_state(upload_id, state)
            return

        # GSEARCH_GEMINI_CHUNK_SIZE / GSEARCH_GEMINI_MAX_INFLIGHT are gone: they were a
        # second name for GEMINI_BATCH_SHARD_SIZE / _MAX_INFLIGHT with identical defaults.
        batch_model = llm_batch.batch_model()
        chunk_size = llm_batch.shard_size()
        max_inflight = llm_batch.max_inflight()
        chunks = [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]
        # Row -> owning chunk id, so a chunk failure can be traced back to its rows below.
        chunk_id_by_ridx: dict[int, int] = {}
        for _cid, _chunk_items in enumerate(chunks):
            for _key, _ in _chunk_items:
                _ridx = row_index_by_key.get(_key)
                if _ridx is not None:
                    chunk_id_by_ridx[_ridx] = _cid
        prior = {int(c.get("chunk_id")): str(c.get("job_name") or "")
                 for c in (existing_chunks or []) if isinstance(c, dict) and c.get("chunk_id") is not None}
        _log_gemini_batch(upload_id, f"chunking total_rows={len(items)} chunk_size={chunk_size} "
                                     f"chunks={len(chunks)} max_inflight={max_inflight}")

        sem = asyncio.Semaphore(max_inflight)

        async def _guarded(cid: int, chunk_items):
            # Skip chunks already succeeded on a prior pass (resume).
            done = next((c for c in (existing_chunks or [])
                         if isinstance(c, dict) and c.get("chunk_id") == cid and c.get("status") == "succeeded"), None)
            if done:
                return {"chunk_id": cid, "job_name": done.get("job_name"), "status": "succeeded",
                        "error": None, "parsed_by_row": {}, "usage": {}}
            async with sem:
                return await _run_one_gemini_chunk(
                    upload_id,
                    cid,
                    chunk_items,
                    prior.get(cid, ""),
                    batch_model,
                    driver_generation,
                )

        results = await asyncio.gather(*[_guarded(i, c) for i, c in enumerate(chunks)])

        # Apply all parsed results, set chunk + aggregate status, persist once -> gated finalize.
        async with get_upload_lock(upload_id):
            state = await read_upload_artifact(upload_id, "state")
            if _batch_deleted_by_user(state):
                _log_gemini_batch(upload_id, "discarding completed chunk results after user deletion")
                return
            if _batch_generation(state) != driver_generation:
                _log_gemini_batch(upload_id, "discarding completed chunk results from stale generation")
                return
            deleted_jobs = {
                str(value).strip()
                for value in (state.get("batch_deleted_job_names") or [])
                if str(value).strip()
            }
            results = [
                ({
                    **result,
                    "job_name": None,
                    "status": "failed",
                    "error": "Remote batch deleted by user",
                    "parsed_by_row": {},
                    "usage": {},
                } if str(result.get("job_name") or "").strip() in deleted_jobs else result)
                for result in results
            ]
            parsed_all, usage_all = {}, {}
            for r in results:
                parsed_all.update(r.get("parsed_by_row") or {})
                usage_all.update(r.get("usage") or {})
            total_prompt = total_cand = 0
            for row in state.get("rows", []):
                if not isinstance(row, dict):
                    continue
                ridx = int(row.get("row_index", 0) or 0)
                parsed = parsed_all.get(ridx)
                if not isinstance(parsed, dict):
                    continue
                usage = usage_all.get(ridx) or {}
                total_prompt += int(usage.get("promptTokenCount", 0) or 0)
                total_cand += int(usage.get("candidatesTokenCount", 0) or 0)
                _apply_batch_parsed_to_row(row, parsed, usage, batch_model)
            # Rows that WERE seeded into the batch (i.e. present in chunk_id_by_ridx,
            # which is built from the exact same items as _build_batch_items_for_state)
            # but whose chunk never produced a parsed decision for them (the chunk
            # failed outright, or the row's key was simply missing from an otherwise
            # successful chunk's response) never reached _apply_batch_parsed_to_row
            # above -> that's a genuine Gemini batch error, not a business not-found.
            # Rows that DID get a parsed dict were already fully decided (found or
            # not_found, both now status="completed") by the loop above and must not
            # be re-touched here, even though not_found rows also have no website.
            # Rows never seeded normally stay untouched. The one exception is an exact
            # pending sentinel: that row completed after the input snapshot and must not
            # remain pending once this batch is terminal.
            results_by_chunk_id = {r["chunk_id"]: r for r in results}
            _pending_sentinel = "Pending Gemini batch post-processing decision."
            for row in state.get("rows", []):
                if not isinstance(row, dict) or row.get("status") != "completed":
                    continue
                if str((row.get("result") or {}).get("official_website") or "").strip():
                    continue
                ridx = int(row.get("row_index", 0) or 0)
                if ridx not in chunk_id_by_ridx:
                    if row.get("error") == _pending_sentinel:
                        row["status"] = "failed"
                        row["outcome"] = _outcomes.OUTCOME_ERROR
                        row["error_source"] = _outcomes.SRC_GEMINI
                        row["error_category"] = _outcomes.CAT_INTERNAL
                        row["error"] = "Gemini batch missed row after its input snapshot."
                    continue
                if isinstance(parsed_all.get(ridx), dict):
                    continue  # already decided (found/not_found) above
                chunk_result = results_by_chunk_id.get(chunk_id_by_ridx.get(ridx, -1)) or {}
                chunk_error = str(chunk_result.get("error") or "gemini batch chunk failed")
                row["status"] = "failed"
                row["outcome"] = _outcomes.OUTCOME_ERROR
                row["error_source"] = _outcomes.SRC_GEMINI
                row["error_category"] = _outcomes.categorize_http_error(None, chunk_error)
                current_error = row.get("error")
                if not current_error or current_error == _pending_sentinel:
                    row["error"] = f"Gemini batch chunk failed: {chunk_error}"
            n_ok = sum(1 for r in results if r["status"] == "succeeded")
            n_fail = sum(1 for r in results if r["status"] != "succeeded")
            agg = "succeeded" if n_fail == 0 else ("failed" if n_ok == 0 else "completed_with_errors")
            batch_usage = {"promptTokenCount": total_prompt, "candidatesTokenCount": total_cand}
            state["gemini_batch"] = {
                "status": agg, "started_at": state.get("gemini_batch", {}).get("started_at"),
                "completed_at": _now_iso(),
                "generation": driver_generation,
                "chunks": [{"chunk_id": r["chunk_id"], "job_name": r["job_name"],
                            "status": r["status"], "error": r["error"]} for r in results],
                "usage": batch_usage, "batch_cost_usd": calculate_gemini_batch_cost_usd(batch_usage),
                "error": None if n_fail == 0 else f"{n_fail}/{len(results)} chunk(s) failed",
            }
            await persist_upload_state(upload_id, state)
        _log_gemini_batch(upload_id, f"all_chunks_done ok={n_ok} failed={n_fail} agg={agg}")
    except Exception as exc:
        _log_gemini_batch(upload_id, f"driver failed error_type={type(exc).__name__} error={repr(exc)}")
        async with get_upload_lock(upload_id):
            try:
                state = await read_upload_artifact(upload_id, "state")
                if (_batch_deleted_by_user(state)
                        or _batch_generation(state) != driver_generation):
                    return
                state["gemini_batch"] = {**state.get("gemini_batch", {}), "status": "failed",
                                         "completed_at": _now_iso(), "error": str(exc)}
                await persist_upload_state(upload_id, state)
            except Exception:
                pass
    finally:
        gemini_batch_tasks.pop(upload_id, None)


async def maybe_start_gemini_batch_for_upload(upload_id: str, state: dict[str, Any]) -> None:
    if not _batch_postprocess_enabled_for(str(state.get("pipeline") or "")):
        return
    if state.get("stopped_by_user_at"):
        # /uploads/{id}/stop terminalizes the upload; don't launch a batch for it.
        # A subsequent retry-failed-rows clears the marker and re-enables this.
        return
    if state.get("status") not in {"completed", "completed_with_errors"}:
        return
    gemini_batch_meta = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
    if _batch_deleted_by_user(state):
        # A destructive Batch Manager action is durable. Explicit failed-row
        # retry replaces this metadata block and intentionally re-enables batch.
        return
    if gemini_batch_meta.get("status") in {
        "queued", "running", "cancel_requested", "cancelled", "succeeded",
        "completed_with_errors", "failed", "skipped",
    }:
        return
    if upload_id in gemini_batch_tasks:
        return
    state["gemini_batch"] = {
        "status": "queued",
        "generation": _batch_generation(state),
        "queued_at": _now_iso(),
        "job_name": None,
        "error": None,
    }
    await persist_upload_state(upload_id, state)
    gemini_batch_tasks[upload_id] = asyncio.create_task(run_gemini_batch_for_upload(upload_id))


async def maybe_resume_gemini_batch_for_upload(upload_id: str, state: dict[str, Any]) -> None:
    if not _batch_postprocess_enabled_for(str(state.get("pipeline") or "")):
        return
    gemini_batch_meta = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
    status = str(gemini_batch_meta.get("status") or "")
    job_name = str(gemini_batch_meta.get("job_name") or "").strip()
    if status != "running":
        return
    if not job_name:
        return
    existing_task = gemini_batch_tasks.get(upload_id)
    if existing_task and not existing_task.done():
        return
    gemini_batch_tasks[upload_id] = asyncio.create_task(run_gemini_batch_for_upload(upload_id))


async def maybe_reconcile_gemini_batch_status(upload_id: str, state: dict[str, Any]) -> dict[str, Any]:
    if not _batch_postprocess_enabled_for(str(state.get("pipeline") or "")):
        return state
    gemini_batch_meta = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
    local_status = str(gemini_batch_meta.get("status") or "").strip()
    job_name = str(gemini_batch_meta.get("job_name") or "").strip()
    started_at_dt = _parse_iso_datetime(gemini_batch_meta.get("started_at"))
    startup_timeout_sec = max(30.0, _get_float_env("GEMINI_BATCH_STARTUP_TIMEOUT_SEC", 180.0))

    # Chunked runs: the worker's reconcile_pending_gemini_batches owns re-poll.
    # Do NOT apply the single-job stale-guard or single-job re-poll here.
    if isinstance(gemini_batch_meta.get("chunks"), list) and gemini_batch_meta.get("chunks"):
        return state

    # Guard against stale "running" states when no remote job was ever recorded.
    if local_status == "running" and not job_name and started_at_dt is not None:
        age_sec = (datetime.now(timezone.utc) - started_at_dt).total_seconds()
        if age_sec >= startup_timeout_sec:
            async with get_upload_lock(upload_id):
                try:
                    latest = await read_upload_artifact(upload_id, "state")
                    latest_batch = latest.get("gemini_batch") if isinstance(latest.get("gemini_batch"), dict) else {}
                    latest_batch["status"] = "failed"
                    latest_batch["completed_at"] = _now_iso()
                    latest_batch["error"] = (
                        f"Gemini batch auto-failed: running with no job_name for {int(age_sec)}s "
                        f"(timeout={int(startup_timeout_sec)}s)."
                    )
                    latest["gemini_batch"] = latest_batch
                    await persist_upload_state(upload_id, latest)
                    _log_row_stage(
                        "batch.reconcile",
                        (
                            f"auto_failed_stale_running_without_job_name age_sec={int(age_sec)} "
                            f"timeout_sec={int(startup_timeout_sec)}"
                        ),
                        upload_id=upload_id,
                        row_index=None,
                        level="WARN",
                    )
                    return latest
                except Exception:
                    return state

    if local_status not in {"queued", "running", "cancel_requested"}:
        return state
    if not job_name:
        return state

    try:
        batch_obj = await asyncio.to_thread(_gemini_batch_get_sync, job_name)
    except Exception:
        return state

    live_state = _gemini_batch_state_name(batch_obj)
    done_flag = bool(batch_obj.get("done"))
    error_obj = batch_obj.get("error")
    resolved_status = _derive_ui_batch_status(
        live_state=live_state,
        done_flag=done_flag,
        error_obj=error_obj,
        local_status=local_status,
    )

    if resolved_status == local_status:
        return state

    async with get_upload_lock(upload_id):
        try:
            latest = await read_upload_artifact(upload_id, "state")
            latest_batch = latest.get("gemini_batch") if isinstance(latest.get("gemini_batch"), dict) else {}
            latest_batch["status"] = resolved_status
            latest_batch["job_name"] = job_name
            if done_flag:
                latest_batch["completed_at"] = _now_iso()
            if error_obj:
                latest_batch["error"] = json.dumps(error_obj, ensure_ascii=True)
            elif resolved_status in {"succeeded", "running", "cancelled"}:
                latest_batch["error"] = None
            latest["gemini_batch"] = latest_batch
            await persist_upload_state(upload_id, latest)
            _log_row_stage(
                "batch.reconcile",
                (
                    f"job_name={_short_text(job_name, 120)!r} "
                    f"local_status={local_status!r} resolved_status={resolved_status!r} "
                    f"live_state={_short_text(live_state, 80)!r} done={done_flag}"
                ),
                upload_id=upload_id,
                row_index=None,
            )
            return latest
        except Exception:
            return state


# CSV input parsing (_normalize_header/_validate_canonical_upload_csv/parse_csv_rows/
# parse_firmographics_csv_rows) lives in csv_input.py; re-imported at the top.


def build_processing_timing_summary(rows: Any) -> dict[str, Any]:
    total_seconds = 0.0
    count = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        result_obj = row.get("result") if isinstance(row.get("result"), dict) else row.get("output")
        if not isinstance(result_obj, dict):
            continue
        context_obj = result_obj.get("context") if isinstance(result_obj.get("context"), dict) else {}
        timing_obj = context_obj.get("timing") if isinstance(context_obj.get("timing"), dict) else {}
        if "total_seconds" not in timing_obj:
            continue
        seconds = _as_float(timing_obj.get("total_seconds"), -1.0)
        if seconds < 0:
            continue
        total_seconds += seconds
        count += 1
    return {
        "processing_seconds_total": round(total_seconds, 3),
        "processing_seconds_avg": round(total_seconds / count, 3) if count else 0.0,
        "processing_seconds_count": count,
    }


def _run_elapsed_seconds(state: dict[str, Any]) -> Optional[float]:
    """Wall-clock seconds from run start (created_at) to completion.

    Completion = the latest terminal timestamp among the rows' status_updated_at and the
    Gemini batch's completed_at — a stable value that does NOT drift when later
    reconciler/finalize passes re-persist the state. While the run is still non-terminal,
    measure up to 'now' so the UI shows live elapsed. This is the true wall-clock span,
    NOT the sum of per-row work times (which overcounts wildly under parallel workers).
    Returns None if created_at is unparseable (legacy states) so callers can fall back.
    """
    start = _parse_iso_datetime(state.get("created_at"))
    if start is None:
        return None
    ends: list[Any] = []
    for r in state.get("rows", []) or []:
        if isinstance(r, dict):
            d = _parse_iso_datetime(r.get("status_updated_at"))
            if d is not None:
                ends.append(d)
    gb = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
    d = _parse_iso_datetime(gb.get("completed_at"))
    if d is not None:
        ends.append(d)
    terminal = state.get("status") in {"completed", "completed_with_errors"}
    if terminal:
        # Prefer the last row/batch terminal timestamp; fall back to the last persist
        # (updated_at) for states that predate per-row status_updated_at.
        end = max(ends) if ends else _parse_iso_datetime(state.get("updated_at"))
    else:
        end = datetime.now(timezone.utc)
    if end is None:
        return None
    return round(max((end - start).total_seconds(), 0.0), 3)


def summarize_upload_state(state: dict[str, Any]) -> dict[str, Any]:
    rows = state.get("rows", [])
    total = len(rows)
    processed = sum(1 for r in rows if r.get("status") in {"completed", "failed"})
    success = sum(1 for r in rows if r.get("status") == "completed")
    failed = sum(1 for r in rows if r.get("status") == "failed")

    if processed == 0:
        status = "queued"
    elif processed < total:
        status = "processing"
    elif failed == 0:
        status = "completed"
    else:
        status = "completed_with_errors"

    state["status"] = status
    state["total_rows"] = total
    state["processed_rows"] = processed
    state["success_rows"] = success
    state["failed_rows"] = failed

    pipeline_name = str(state.get("pipeline") or "")

    def _outcome_of(r: dict[str, Any]) -> Optional[str]:
        oc = r.get("outcome")
        if oc:
            return oc
        # Derive for rows without an explicit outcome (out-of-scope pipelines,
        # or peripheral status="failed" paths like user-stop/redelivery-drop).
        # The "did it produce a result" test lives in reporting and is SHARED
        # with its _derive_outcome twin: firmographics answers it differently (its
        # official_website is the input echoed back), and two copies would drift.
        if r.get("status") == "completed":
            return (_outcomes.OUTCOME_FOUND
                    if reporting.row_produced_a_result(r.get("result"), pipeline_name)
                    else _outcomes.OUTCOME_NOT_FOUND)
        if r.get("status") == "failed":
            return _outcomes.OUTCOME_ERROR
        return None

    outcome_counts = {"found": 0, "not_found": 0, "errored": 0}
    for r in rows:
        if not isinstance(r, dict):
            continue
        oc = _outcome_of(r)
        if oc == _outcomes.OUTCOME_FOUND:
            outcome_counts["found"] += 1
        elif oc == _outcomes.OUTCOME_NOT_FOUND:
            outcome_counts["not_found"] += 1
        elif oc == _outcomes.OUTCOME_ERROR:
            outcome_counts["errored"] += 1
    state["outcome_counts"] = outcome_counts

    timing = build_processing_timing_summary(rows)
    elapsed = _run_elapsed_seconds(state)
    # Total = wall-clock start→completion. The per-row sum (timing[...]) overcounts
    # under parallel workers, so only fall back to it when created_at is missing.
    # Avg/row stays the mean per-row work time.
    state["processing_seconds_total"] = (
        elapsed if elapsed is not None else timing["processing_seconds_total"])
    state["processing_seconds_avg"] = timing["processing_seconds_avg"]
    state["processing_seconds_count"] = timing["processing_seconds_count"]
    state["updated_at"] = _now_iso()
    return state


def _normalize_failure_error(error_text: str) -> str:
    text = (error_text or "").strip()
    if not text:
        return "Unknown/empty error"
    lowered = text.lower()
    if "official website not found after gemini batch post-processing" in lowered:
        return "No official website after Gemini batch post-processing"
    if "official website not found; target blocked/unreachable or no valid result" in lowered:
        return "No official website from initial search pipeline"
    if lowered.startswith("dropped after redelivery failure:"):
        return "Worker redelivery drop"
    if lowered.startswith("auto-failed stale processing row"):
        return "Stale processing timeout auto-fail"
    if lowered.startswith("auto-finalized orphaned row"):
        return "Orphan upload auto-finalize"
    if lowered.startswith("queue publish failed:"):
        return "Queue publish failed"
    return text


def build_failure_analysis(state: dict[str, Any], sample_limit: int = 20) -> dict[str, Any]:
    rows = state.get("rows", []) if isinstance(state.get("rows"), list) else []
    total_rows = len(rows)
    failed_rows: list[dict[str, Any]] = []
    error_counts: dict[str, int] = {}
    search_attempt_error_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    no_official_website_failed = 0
    sample_failed: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("status") != "failed":
            continue
        failed_rows.append(row)
        result_obj = row.get("result") if isinstance(row.get("result"), dict) else {}
        official_website = str(result_obj.get("official_website") or "").strip()
        if not official_website:
            no_official_website_failed += 1

        normalized_error = _normalize_failure_error(str(row.get("error") or ""))
        error_counts[normalized_error] = int(error_counts.get(normalized_error, 0) or 0) + 1

        error_source = row.get("error_source")
        if error_source:
            source_counts[str(error_source)] = int(source_counts.get(str(error_source), 0) or 0) + 1
        error_category = row.get("error_category")
        if error_category:
            category_counts[str(error_category)] = int(category_counts.get(str(error_category), 0) or 0) + 1

        context_obj = result_obj.get("context") if isinstance(result_obj.get("context"), dict) else {}
        attempts = context_obj.get("search_attempts") if isinstance(context_obj.get("search_attempts"), list) else []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            attempt_error = str(attempt.get("error") or "").strip()
            if not attempt_error:
                continue
            search_attempt_error_counts[attempt_error] = int(search_attempt_error_counts.get(attempt_error, 0) or 0) + 1

        if len(sample_failed) < max(1, int(sample_limit)):
            sample_failed.append(
                {
                    "row_index": row.get("row_index"),
                    "company_name": row.get("company_name"),
                    "country": row.get("country"),
                    "error_source": row.get("error_source"),
                    "error_category": row.get("error_category"),
                    "error": row.get("error"),
                    "official_website": official_website or None,
                    "status_updated_at": row.get("status_updated_at"),
                }
            )

    def _sort_counts(counts: dict[str, int], top_n: int = 20, key_name: str = "reason") -> list[dict[str, Any]]:
        items = sorted(counts.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))
        return [{key_name: k, "count": v} for k, v in items[:top_n]]

    return {
        "upload_id": state.get("upload_id"),
        "status": state.get("status"),
        "gemini_batch": state.get("gemini_batch"),
        "total_rows": total_rows,
        "failed_rows": len(failed_rows),
        "failed_rate_pct": round((len(failed_rows) / total_rows) * 100, 2) if total_rows else 0.0,
        "failed_missing_official_website": no_official_website_failed,
        "error_buckets": _sort_counts(error_counts),
        "search_attempt_error_buckets": _sort_counts(search_attempt_error_counts),
        "by_source": _sort_counts(source_counts, key_name="source"),
        "by_category": _sort_counts(category_counts, key_name="category"),
        "sample_failed_rows": sample_failed,
    }


def _upload_file_links(upload_id: str, company_name: str = "", pipeline: str = "") -> dict[str, str]:
    bucket = os.getenv("S3_BUCKET")
    pipe = pipeline or ""
    if pipe == PIPELINE_RELATIONSHIP:
        # No state.json / output.json: this pipeline keeps no state dict and no local
        # disk — S3 object presence IS its state. Advertising them produced two links
        # to objects that never exist.
        names = ["confirmed_relation.csv", "notconfirmed_relation.csv",
                 "report.json", "run.log"]
    else:
        names = ["state.json", "output.json"]
        if pipe in REPORTING_PIPELINES:
            names += ["found.csv", "notFound.csv", "report.json", "run.log"]
    if bucket:
        prefix = _resolved_upload_s3_prefix(upload_id, company_name, pipe)
        return {name: f"s3://{bucket}/{prefix}/{name}" for name in names}
    base = _find_upload_dir(upload_id)
    return {name: str(base / name) for name in names}


def _reporting_result_names(pipeline: str) -> list[str]:
    if pipeline == PIPELINE_RELATIONSHIP:
        return ["confirmed_relation.csv", "notconfirmed_relation.csv",
                "report.json", "run.log"]
    if pipeline not in REPORTING_PIPELINES:
        return []
    return ["found.csv", "notFound.csv", "report.json", "run.log"]


def _list_available_reporting_files_s3_sync(
    run_prefix: str,
    expected: list[str],
) -> set[str]:
    bucket = os.getenv("S3_BUCKET")
    if not bucket or not expected:
        return set()
    prefix = f"{run_prefix.rstrip('/')}/"
    try:
        response = get_s3_client().list_objects_v2(
            Bucket=bucket,
            Prefix=prefix,
            Delimiter="/",
            MaxKeys=1000,
        )
    except Exception:
        return set()
    expected_keys = {f"{prefix}{name}": name for name in expected}
    return {
        expected_keys[key]
        for obj in response.get("Contents", [])
        if (key := str(obj.get("Key") or "")) in expected_keys
    }


async def _available_reporting_files(
    upload_id: str,
    company_name: str,
    pipeline: str,
) -> list[str]:
    """Return reporting artifacts that exist locally or in configured S3 storage."""
    expected = _reporting_result_names(pipeline)
    if not expected:
        return []
    upload_dir = _find_upload_dir(upload_id)
    available = [name for name in expected if (upload_dir / name).exists()]
    if not os.getenv("S3_BUCKET"):
        return available

    missing = [name for name in expected if name not in available]
    if not missing:
        return available
    cached_prefix = _restore_s3_run_prefix(upload_id)
    normalized_prefix = _upload_s3_prefix(upload_id, company_name, pipeline)
    run_prefix = _resolved_upload_s3_prefix(upload_id, company_name, pipeline)
    present_in_s3 = await asyncio.to_thread(
        _list_available_reporting_files_s3_sync,
        run_prefix,
        missing,
    )
    if not present_in_s3 and cached_prefix is None:
        legacy_state_key = await asyncio.to_thread(
            _find_s3_upload_key_sync, upload_id, "state.json")
        if legacy_state_key:
            actual_prefix = legacy_state_key.rsplit("/", 1)[0]
            _remember_s3_run_prefix(upload_id, actual_prefix)
            if actual_prefix != normalized_prefix:
                present_in_s3 = await asyncio.to_thread(
                    _list_available_reporting_files_s3_sync,
                    actual_prefix,
                    missing,
                )
    return [name for name in expected if name in available or name in present_in_s3]


def update_summary_cache(upload_id: str, state: dict[str, Any]) -> None:
    try:
        summary = summarize_upload_state(dict(state))
        state_pipeline = str(summary.get("pipeline") or "")
        file_links = _upload_file_links(upload_id, str(summary.get("company_name") or ""), str(summary.get("pipeline") or ""))
        upload_summaries_cache[upload_id] = {
            "upload_id": summary.get("upload_id"),
            "pipeline": state_pipeline,
            "status": summary.get("status"),
            "gemini_batch": summary.get("gemini_batch"),
            "created_at": summary.get("created_at"),
            "updated_at": summary.get("updated_at"),
            "total_rows": summary.get("total_rows", 0),
            "processed_rows": summary.get("processed_rows", 0),
            "success_rows": summary.get("success_rows", 0),
            "failed_rows": summary.get("failed_rows", 0),
            "processing_seconds_total": summary.get("processing_seconds_total", 0.0),
            "processing_seconds_avg": summary.get("processing_seconds_avg", 0.0),
            "processing_seconds_count": summary.get("processing_seconds_count", 0),
            "file_links": file_links,
            "status_url": f"/uploads/{upload_id}/status",
            "output_url": f"/uploads/{upload_id}/output",
            "output_csv_url": f"/uploads/{upload_id}/output?format=csv",
            "output_xlsx_url": f"/uploads/{upload_id}/output?format=xlsx",
        }
    except Exception:
        pass


def _update_supabase_run(state: dict[str, Any]) -> bool:
    """Best-effort Supabase ``runs`` row update at upload terminal status (Task 14).

    Maps only what the upload state actually tracks (row success/failed counters,
    timing, state/output artifact locations). Wrapped entirely in try/except so
    Supabase bookkeeping can NEVER break the worker. Returns True only when the
    runs row was actually updated, so callers can mark the snapshot as synced.
    """
    try:
        run_db_id = state.get("run_db_id")
        if not run_db_id:
            return False
        from app.services.companies import get_company_service

        svc = get_company_service()
        if svc is None:
            return False
        upload_id = str(state.get("upload_id") or "")
        file_links = _upload_file_links(upload_id, str(state.get("company_name") or ""), str(state.get("pipeline") or ""))
        extra: dict[str, Any] = {}
        success_count = state.get("success_rows")
        failed_count = state.get("failed_rows")
        if str(state.get("pipeline") or "") in COST_SUMMARY_PIPELINES:
            results = reporting.state_to_entity_results(state)
            summ = reporting.build_summary(state, results)
            extra = {
                "websites_found": summ["websites_found"],
                "websites_not_found": summ["websites_not_found"],
                "cost": summ["cost"],
                "token_usage": summ["token_usage"],
            }
            # success/failed now follow the 3-way row outcome (found/not_found/
            # errored) instead of the old 2-way success/failed split: a business
            # not_found row (no website, but no error) no longer counts as failed
            # — only a genuine per-row error does.
            ob = summ.get("outcome_breakdown") or {}
            success_count = ob.get("found", summ["websites_found"])
            failed_count = ob.get("errored", 0)
        return svc.update_run(
            run_db_id,
            status=str(state.get("status") or ""),
            success_count=success_count,
            failed_count=failed_count,
            duration_seconds=state.get("processing_seconds_total"),
            file_links=file_links,
            finished_at=_now_iso(),
            **extra,
        )
    except Exception as exc:
        print(
            f"[supabase] run update failed for upload {state.get('upload_id')} "
            f"(worker unaffected): {type(exc).__name__}: {exc}"
        )
        return False


def _mark_supabase_run_running(state: dict[str, Any]) -> bool:
    """Best-effort one-shot Supabase ``runs`` sync when rows start processing (spec §4).

    Maps upload state "processing" to runs.status "running" and stamps
    ``started_at``. Wrapped entirely in try/except so Supabase bookkeeping can
    NEVER break the worker.
    """
    try:
        run_db_id = state.get("run_db_id")
        if not run_db_id:
            return False
        from app.services.companies import get_company_service

        svc = get_company_service()
        if svc is None:
            return False
        return svc.update_run(run_db_id, status="running", started_at=_now_iso())
    except Exception as exc:
        print(
            f"[supabase] run 'running' update failed for upload {state.get('upload_id')} "
            f"(worker unaffected): {type(exc).__name__}: {exc}"
        )
        return False


def _should_sync_running(state: dict[str, Any]) -> bool:
    """True when this snapshot is the upload's first exit from 'queued'.

    Fires only for tracked uploads (``run_db_id``) whose state just became
    "processing" and that haven't already synced (``supabase_running_marker``).
    Terminal states never match — the terminal sync in persist_upload_state
    handles those.
    """
    return (
        bool(state.get("run_db_id"))
        and state.get("status") == "processing"
        and not state.get("supabase_running_marker")
    )


def _should_sync_supabase(state: dict[str, Any], marker: str) -> bool:
    """True when this terminal snapshot still needs a Supabase ``runs`` sync.

    Skips snapshots already synced (``supabase_sync_marker``) AND snapshots whose
    sync already failed (``supabase_sync_failed_marker``). Trade-off: a failed
    sync is retried at most once per distinct terminal snapshot — a permanently
    down Supabase doesn't re-stall every persist call (update_run retries 3x with
    sleeps), but a later terminal state with different counters retries once.
    """
    return (
        state.get("supabase_sync_marker") != marker
        and state.get("supabase_sync_failed_marker") != marker
    )


def _notify_slack_terminal(state: dict[str, Any]) -> None:
    """Best-effort Slack ping when an upload reaches a terminal state. Never raises."""
    try:
        from app.core import notify

        status = str(state.get("status") or "")
        pipeline = notify.pipeline_label(state.get("pipeline") or "")
        # relationship state counters/total_rows are PAIR-level; the Slack ping
        # should match the CSVs the user downloads (ORIGINAL-ROW-level), so use
        # the original row count when this is a relationship upload.
        relationship_meta = state.get("relationship")
        total_rows = state.get("total_rows")
        if isinstance(relationship_meta, dict):
            total_rows = relationship_meta.get("row_count_original", total_rows)
        common = {
            "pipeline": pipeline,
            "company": state.get("company_name"),
            "run_ref": str(state.get("upload_id") or ""),
            "total_rows": total_rows,
            "duration_seconds": state.get("processing_seconds_total"),
        }
        if status == "failed":
            notify.notify_run_failed(error=state.get("error") or "run failed", **common)
        else:  # completed | completed_with_errors
            # gsearch and gmaps both track SerpWow searches + Gemini tokens/cost via
            # the same reporting.build_summary — surface them in the ping like
            # AI Mode does. Other SerpWow pipelines have none, so omit.
            extra: dict[str, Any] = {}
            is_reporting = str(state.get("pipeline") or "") in COST_SUMMARY_PIPELINES
            if is_reporting:
                try:
                    gs = reporting.build_summary(
                        state, reporting.state_to_entity_results(state))
                    tu = gs.get("token_usage") or {}
                    ob = gs.get("outcome_breakdown") or {}
                    eb = gs.get("error_breakdown") or {}
                    # 3-way outcome trio replaces the old success/failed pair for
                    # in-scope pipelines: found/not_found/errored instead of a
                    # binary success/failed that couldn't distinguish "no website
                    # found" from "the row errored out".
                    found = ob.get("found")
                    not_found = ob.get("not_found")
                    errored = ob.get("errored")
                    # scrape.do-billed pipelines (gmaps) report requests + credits;
                    # SerpWow ones keep reporting per-search USD.
                    # Truthy, not "is not None": _build_cost emits these keys as 0 for
                    # every SerpWow pipeline too. Checks failed/credits as well so a run
                    # whose every call failed still reports as scrape.do.
                    _is_scrapedo = bool(gs["cost"].get("scrapedo_requests")
                                        or gs["cost"].get("scrapedo_credits")
                                        or gs["cost"].get("scrapedo_failed_requests"))
                    extra = {
                        "searches": (gs["cost"]["scrapedo_requests"] if _is_scrapedo
                                     else gs["cost"]["serpwow_searches"]),
                        "search_label": ("Scrape.do requests" if _is_scrapedo
                                         else "SerpWow searches"),
                        "credits": gs["cost"]["scrapedo_credits"] if _is_scrapedo else None,
                        "tokens": tu.get("total_tokens"),
                        "input_tokens": tu.get("prompt_tokens"),
                        "output_tokens": tu.get("completion_tokens"),
                        "cost_usd": gs["cost"]["total_usd"],
                        "llm_cost_usd": gs["cost"]["llm_usd"],
                        # None for scrape.do runs so the ping shows credits instead of
                        # a misleading $0.00 per-search cost.
                        "serpwow_cost_usd": None if _is_scrapedo else gs["cost"]["serpwow_usd"],
                        "found": found,
                        "not_found": not_found,
                        "errored": errored,
                        "error_sources": eb.get("by_source") or {},
                    }
                except Exception:
                    extra = {}
                    is_reporting = False
            notify.notify_run_complete(
                status=status,
                **({} if is_reporting else {
                    "success": state.get("success_rows"),
                    "failed": state.get("failed_rows"),
                }),
                **common,
                **extra,
            )
    except Exception as exc:
        print(f"[slack] notify failed for upload {state.get('upload_id')} "
              f"(worker unaffected): {type(exc).__name__}: {exc}")


async def _finalize_serpwow_outputs(upload_id: str, state: dict[str, Any]) -> None:
    """Write found/notFound/report/run.log for a terminal gsearch upload and mirror them
    to S3. Best-effort: logs + swallows everything, never raises."""
    try:
        upload_dir = _find_upload_dir(upload_id)
        paths = await asyncio.to_thread(reporting.write_outputs, upload_dir, state)
    except Exception as exc:
        print(f"[serpwow] reporting failed for {upload_id}: {type(exc).__name__}: {exc}")
        return
    try:
        error_paths = await asyncio.to_thread(_write_error_dumps, upload_dir, state)
    except Exception as exc:
        print(f"[serpwow] error-dump failed for {upload_id}: {type(exc).__name__}: {exc}")
        error_paths = {}
    if not os.getenv("S3_BUCKET"):
        return
    from app.core import s3 as core_s3
    pipeline = str(state.get("pipeline") or PIPELINE_GSEARCH)
    prefix = _resolved_upload_s3_prefix(
        upload_id, str(state.get("company_name") or ""), pipeline)
    for name, path in paths.items():
        try:
            await asyncio.to_thread(core_s3.upload_file, path, f"{prefix}/{name}")
        except Exception as exc:
            print(f"[serpwow] S3 mirror failed for {name} ({upload_id}): {type(exc).__name__}: {exc}")
    for name, path in error_paths.items():
        try:
            await asyncio.to_thread(core_s3.upload_file, path, f"{prefix}/errors/{name}")
        except Exception as exc:
            print(f"[serpwow] S3 mirror failed for errors/{name} ({upload_id}): {type(exc).__name__}: {exc}")


async def persist_upload_state(upload_id: str, state: dict[str, Any]) -> None:
    deletion_tombstone = await _read_batch_deletion_tombstone(upload_id, state)
    if deletion_tombstone:
        _apply_batch_deletion_tombstone(state, deletion_tombstone)
    state = summarize_upload_state(state)
    # gsearch batch mode: rows reach "completed" before the Gemini batch (the LLM
    # confidence step) runs. Defer the terminal side-effects (Supabase 'completed'
    # sync, Slack ping, output-file finalize) until the batch is terminal so we
    # report completion ONCE, with the final post-batch numbers (like AI Mode).
    batch_pending = _batch_postprocess_pending(state)
    if _should_sync_running(state):
        # One-shot 'running' sync: the marker is set regardless of outcome so a
        # down Supabase (update_run retries 3x with sleeps) can't re-stall every
        # subsequent persist; the terminal sync below has its own retry story.
        state["supabase_running_marker"] = True
        await asyncio.to_thread(_mark_supabase_run_running, dict(state))
    if (not batch_pending) and state.get("status") in {"completed", "completed_with_errors", "failed"}:
        # Sync once per distinct terminal snapshot (retries can re-open an upload
        # and re-complete it with different counters; resync then).
        marker = (
            f"{state.get('status')}:{state.get('success_rows')}:{state.get('failed_rows')}"
        )
        if state.get("run_db_id") and _should_sync_supabase(state, marker):
            if await asyncio.to_thread(_update_supabase_run, dict(state)):
                state["supabase_sync_marker"] = marker
            else:
                # Mark the failure so we don't retry this exact snapshot on
                # every persist (see _should_sync_supabase for the trade-off).
                state["supabase_sync_failed_marker"] = marker
        # Slack ping once per distinct terminal snapshot — independent of the
        # Supabase sync above so it still fires when Supabase is unconfigured.
        if state.get("slack_notified_marker") != marker:
            state["slack_notified_marker"] = marker
            await asyncio.to_thread(_notify_slack_terminal, dict(state))
    update_summary_cache(upload_id, state)
    await write_upload_artifact(upload_id, "state", state)

    # Writing output.json on each row update adds significant S3 overhead.
    # Persist it only when upload is complete; otherwise /output builds from state on demand.
    if state["status"] in {"completed", "completed_with_errors"}:
        combined = build_upload_output_payload(state)
        await write_upload_artifact(upload_id, "output", combined)
    if (not batch_pending) and state.get("pipeline") in REPORTING_PIPELINES and state["status"] in {"completed", "completed_with_errors"}:
        await _finalize_serpwow_outputs(upload_id, state)
    await maybe_start_gemini_batch_for_upload(upload_id, state)


async def get_upload_state(upload_id: str) -> dict[str, Any]:
    async with get_upload_lock(upload_id):
        try:
            state = await read_upload_artifact(upload_id, "state")
            update_summary_cache(upload_id, state)
            return state
        except FileNotFoundError as exc:
            raise KeyError(upload_id) from exc
        except Exception as exc:
            # boto3 raises ClientError with NoSuchKey when state is missing.
            if "NoSuchKey" in str(exc):
                raise KeyError(upload_id) from exc
            raise


async def list_upload_summaries(limit: int, pipeline: Optional[str] = None) -> list[dict[str, Any]]:
    cap = max(1, min(int(limit), 500))
    scan_limit = min(2000, max(cap * 4, 50))

    # 1. Try to read local state files first to completely avoid S3 network listing overhead
    local_files = await asyncio.to_thread(_list_local_state_files_sync, scan_limit)
    if local_files:
        upload_ids = [p.parent.name for p in local_files]
    elif os.getenv("S3_BUCKET"):
        # Fallback to S3 listing only if there are no local files (e.g. completely clean /tmp folder)
        keys = await asyncio.to_thread(_list_state_keys_from_s3_sync, scan_limit)
        upload_ids = []
        for key in keys:
            parts = key.split("/")
            if len(parts) >= 3:
                upload_ids.append(parts[-2])
    else:
        upload_ids = []

    seen: set[str] = set()
    summaries: list[dict[str, Any]] = []
    uncached_ids = []

    for upload_id in upload_ids:
        if not upload_id or upload_id in seen:
            continue
        seen.add(upload_id)

        cached = upload_summaries_cache.get(upload_id)
        if cached is not None:
            summaries.append(cached)
        else:
            uncached_ids.append(upload_id)

    if uncached_ids:
        # Limit uncached fetches to satisfy the requested cap and prevent loop starvation
        uncached_ids = uncached_ids[:cap]
        sem = asyncio.Semaphore(5)

        async def _fetch_and_cache(uid: str) -> Optional[dict[str, Any]]:
            async with sem:
                try:
                    state = await read_upload_artifact(uid, "state")
                    update_summary_cache(uid, state)
                    return upload_summaries_cache.get(uid)
                except Exception:
                    return None

        results = await asyncio.gather(*[_fetch_and_cache(uid) for uid in uncached_ids], return_exceptions=True)
        for res in results:
            if isinstance(res, dict):
                summaries.append(res)

    # Filter by pipeline if specified
    if pipeline:
        summaries = [s for s in summaries if s.get("pipeline") == pipeline]

    summaries.sort(key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""), reverse=True)
    return summaries[:cap]


async def update_row_state(
    upload_id: str,
    row_index: int,
    *,
    status: Optional[str] = None,
    error: Optional[str] = None,
    result: Optional[dict[str, Any]] = None,
    raw_response_s3_key: Optional[str] = None,
    outcome: Optional[str] = None,
    error_source: Optional[str] = None,
    error_category: Optional[str] = None,
    degraded_search: Optional[bool] = None,
) -> None:
    async with get_upload_lock(upload_id):
        try:
            state = await read_upload_artifact(upload_id, "state")
        except FileNotFoundError as exc:
            raise KeyError(upload_id) from exc
        except Exception as exc:
            if "NoSuchKey" in str(exc):
                raise KeyError(upload_id) from exc
            raise

        row = next((r for r in state["rows"] if r["row_index"] == row_index), None)
        if row is None:
            return
        now_iso = _now_iso()
        if status is not None:
            # Do not let duplicate/replayed jobs regress a terminal row back into processing.
            if status == "processing" and row.get("status") in {"completed", "failed"}:
                return
            row["status"] = status
            row["status_updated_at"] = now_iso
            if status == "processing":
                row["processing_started_at"] = now_iso
            elif status in {"completed", "failed"}:
                row["processing_started_at"] = None
        if error is not None:
            row["error"] = error
        if result is not None:
            row["result"] = result
        if raw_response_s3_key is not None:
            row["raw_response_s3_key"] = raw_response_s3_key
        if outcome is not None:
            row["outcome"] = outcome
        if error_source is not None:
            row["error_source"] = error_source
        if error_category is not None:
            row["error_category"] = error_category
        if degraded_search is not None:
            row["degraded_search"] = degraded_search
        await persist_upload_state(upload_id, state)


async def maybe_requeue_stuck_queued_rows(upload_id: str, state: dict[str, Any]) -> dict[str, Any]:
    if rabbitmq_exchange is None or rabbitmq_queue is None:
        return state
    if str(state.get("pipeline") or "") not in {
        PIPELINE_FIRMOGRAPHICS,
        PIPELINE_GSEARCH,
    }:
        return state
    if str(state.get("status") or "") not in {"queued", "processing"}:
        return state



    # Both queued rows and stuck processing rows (when queue is empty and no active workers exist) need recovery
    queued_rows = [
        row for row in (state.get("rows") or [])
        if isinstance(row, dict) and row.get("status") in {"queued", "processing"}
    ]
    if not queued_rows:
        return state

    queue_depth = await _get_rabbitmq_queue_depth()
    if queue_depth is None or queue_depth > 0:
        return state

    active_count = await _get_upload_active_row_count(upload_id)
    if active_count > 0:
        return state

    recovery_meta = state.get("queue_recovery") if isinstance(state.get("queue_recovery"), dict) else {}
    cooldown_sec = max(30.0, _get_float_env("QUEUE_RECOVERY_REQUEUE_COOLDOWN_SEC", 120.0))
    last_attempt_dt = _parse_iso_datetime(recovery_meta.get("last_requeue_at"))
    if last_attempt_dt is not None:
        age_sec = (datetime.now(timezone.utc) - last_attempt_dt).total_seconds()
        if age_sec < cooldown_sec:
            return state

    pipeline = str(state.get("pipeline") or "")
    phase = str(state.get("phase") or "all")
    upload_company_name = str(state.get("company_name") or "")
    jobs = [_build_row_job_payload(upload_id, row, pipeline, phase, upload_company_name=upload_company_name) for row in queued_rows]
    republished = 0
    publish_errors: list[tuple[int, str]] = []

    for job in jobs:
        try:
            await publish_job(job)
            republished += 1
        except Exception as exc:
            publish_errors.append((int(job.get("row_index", 0) or 0), str(exc)))

    if republished == 0 and not publish_errors:
        return state

    async with get_upload_lock(upload_id):
        try:
            latest = await read_upload_artifact(upload_id, "state")
        except Exception:
            latest = state

        attempts = int(recovery_meta.get("attempts", 0) or 0) + 1
        latest["queue_recovery"] = {
            "attempts": attempts,
            "last_requeue_at": _now_iso(),
            "last_requeued_rows": republished,
            "last_publish_errors": len(publish_errors),
        }

        if publish_errors:
            by_row = {idx: err for idx, err in publish_errors}
            for row in latest.get("rows", []) or []:
                if not isinstance(row, dict):
                    continue
                row_index = int(row.get("row_index", 0) or 0)
                err = by_row.get(row_index)
                if err is None:
                    continue
                row["status"] = "failed"
                row["status_updated_at"] = _now_iso()
                row["processing_started_at"] = None
                row["error"] = f"Queue recovery publish failed: {err}"

        await persist_upload_state(upload_id, latest)
        _log_row_stage(
            "queue.recovery_requeue",
            (
                f"queue_depth={queue_depth} queued_rows={len(queued_rows)} "
                f"republished={republished} publish_errors={len(publish_errors)} "
                f"attempt={attempts}"
            ),
            upload_id=upload_id,
            row_index=None,
            level="WARN" if publish_errors else "INFO",
        )
        return latest


async def maybe_fail_stale_processing_rows(upload_id: str, state: dict[str, Any]) -> dict[str, Any]:
    timeout_sec = max(30.0, _get_float_env("PROCESSING_STALE_TIMEOUT_SEC", 300.0))
    orphan_timeout_sec = max(30.0, _get_float_env("ORPHAN_UPLOAD_FINALIZE_TIMEOUT_SEC", 180.0))
    now_dt = datetime.now(timezone.utc)
    changed = False
    active_count = await _get_upload_active_row_count(upload_id)
    embedded_worker_enabled = _get_bool_env("ENABLE_EMBEDDED_WORKER", False)
    # In split-process deployments (API + separate worker process), in-memory
    # active-row tracking is not shared. To avoid false mass-failures, auto
    # finalizers are enabled only for embedded-worker mode by default.
    stale_finalizer_enabled = _get_bool_env(
        "ENABLE_STALE_PROCESSING_FINALIZER",
        embedded_worker_enabled,
    )
    orphan_finalizer_enabled = (
        _get_bool_env("ENABLE_ORPHAN_UPLOAD_FINALIZER", False)
        and embedded_worker_enabled
    )
    stale_failed_count = 0
    orphan_failed_count = 0

    async with get_upload_lock(upload_id):
        try:
            latest_state = await read_upload_artifact(upload_id, "state")
        except Exception:
            # Fall back to provided state if latest cannot be loaded.
            latest_state = state

        if stale_finalizer_enabled:
            for row in latest_state.get("rows", []) or []:
                if row.get("status") != "processing":
                    continue
                started_dt = _parse_iso_datetime(row.get("processing_started_at"))
                if started_dt is None:
                    started_dt = _parse_iso_datetime(row.get("status_updated_at"))
                if started_dt is None:
                    started_dt = _parse_iso_datetime(latest_state.get("updated_at"))
                if started_dt is None:
                    continue
                age_sec = (now_dt - started_dt).total_seconds()
                if age_sec < timeout_sec:
                    continue
                row["status"] = "failed"
                row["error"] = (
                    f"Auto-failed stale processing row after {int(age_sec)}s "
                    f"(timeout={int(timeout_sec)}s)."
                )
                row["status_updated_at"] = _now_iso()
                row["processing_started_at"] = None
                changed = True
                stale_failed_count += 1

        # Never orphan-finalize queued rows. This guard is intentionally strict.
        non_terminal_rows = [
            row for row in (latest_state.get("rows", []) or [])
            if row.get("status") == "processing"
        ]
        if orphan_finalizer_enabled and active_count == 0 and non_terminal_rows:
            oldest_non_terminal_dt: Optional[datetime] = None
            for row in non_terminal_rows:
                candidate_dt = (
                    _parse_iso_datetime(row.get("processing_started_at"))
                    or _parse_iso_datetime(row.get("status_updated_at"))
                    or _parse_iso_datetime(latest_state.get("updated_at"))
                    or _parse_iso_datetime(latest_state.get("created_at"))
                )
                if candidate_dt is None:
                    continue
                if oldest_non_terminal_dt is None or candidate_dt < oldest_non_terminal_dt:
                    oldest_non_terminal_dt = candidate_dt

            if oldest_non_terminal_dt is not None:
                orphan_age_sec = (now_dt - oldest_non_terminal_dt).total_seconds()
                if orphan_age_sec >= orphan_timeout_sec:
                    for row in non_terminal_rows:
                        row["status"] = "failed"
                        row["error"] = (
                            f"Auto-finalized orphaned row after {int(orphan_age_sec)}s "
                            f"with no active worker task for upload (timeout={int(orphan_timeout_sec)}s)."
                        )
                        row["status_updated_at"] = _now_iso()
                        row["processing_started_at"] = None
                    changed = True
                    orphan_failed_count += len(non_terminal_rows)

        if changed:
            await persist_upload_state(upload_id, latest_state)
            _log_row_stage(
                "state.auto_finalize",
                (
                    f"stale_failed={stale_failed_count} "
                    f"orphan_failed={orphan_failed_count} "
                    f"active_count={active_count}"
                ),
                upload_id=upload_id,
                row_index=None,
                level="WARN",
            )

    return latest_state


def _build_rabbitmq_url(host: str, port: int, user: str, password: str, vhost: str) -> str:
    encoded_vhost = quote(vhost, safe="")
    return f"amqp://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{encoded_vhost}"


def get_rabbitmq_url() -> str:
    host = os.getenv("RABBITMQ_HOST", "127.0.0.1")
    port = _get_int_env("RABBITMQ_PORT", 5672)
    user = os.getenv("RABBITMQ_USER", "guest")
    password = os.getenv("RABBITMQ_PASS", "guest")
    vhost = os.getenv("RABBITMQ_VHOST", "/")
    return _build_rabbitmq_url(host, port, user, password, vhost)


async def init_rabbitmq() -> None:
    global rabbitmq_connection, rabbitmq_channel, rabbitmq_exchange, rabbitmq_queue, rabbitmq_last_error

    host = os.getenv("RABBITMQ_HOST", "127.0.0.1")
    port = _get_int_env("RABBITMQ_PORT", 5672)
    user = os.getenv("RABBITMQ_USER", "guest")
    password = os.getenv("RABBITMQ_PASS", "guest")
    vhost = os.getenv("RABBITMQ_VHOST", "/")

    primary_url = _build_rabbitmq_url(host, port, user, password, vhost)
    try:
        rabbitmq_connection = await aio_pika.connect_robust(primary_url)
    except Exception as primary_exc:
        # Local dev fallback: try guest/guest on localhost if custom creds are rejected.
        if host in {"127.0.0.1", "localhost"} and not (user == "guest" and password == "guest"):
            fallback_url = _build_rabbitmq_url(host, port, "guest", "guest", vhost)
            try:
                rabbitmq_connection = await aio_pika.connect_robust(fallback_url)
                rabbitmq_last_error = (
                    f"Primary credentials failed ({str(primary_exc)}); "
                    "connected using guest/guest fallback."
                )
            except Exception:
                raise primary_exc
        else:
            raise
    rabbitmq_channel = await rabbitmq_connection.channel()
    await rabbitmq_channel.set_qos(prefetch_count=max(1, _get_int_env("WORKER_CONCURRENCY", 4)))

    exchange_name = os.getenv("RABBITMQ_EXCHANGE", "singleRA_search")
    queue_name = os.getenv("RABBITMQ_QUEUE", "singleRA_search_jobs")
    routing_key = os.getenv("RABBITMQ_ROUTING_KEY", "singleRA.search.validate")

    rabbitmq_exchange = await rabbitmq_channel.declare_exchange(
        exchange_name,
        aio_pika.ExchangeType.DIRECT,
        durable=True,
    )
    rabbitmq_queue = await rabbitmq_channel.declare_queue(queue_name, durable=True)
    await rabbitmq_queue.bind(rabbitmq_exchange, routing_key=routing_key)
    rabbitmq_last_error = None

    # The three S3-only run queues, declared by the PUBLISHER as well as the worker. A
    # direct exchange drops an unroutable message on the floor, so publishing a run before
    # the worker had ever bound its queue lost the message entirely and the run stalled at
    # phase="queued" until the stale-run scan found it. See declare_run_queues.
    try:
        from app.services.serpwow import s3_run_driver

        await s3_run_driver.declare_run_queues(rabbitmq_channel, rabbitmq_exchange)
    except Exception as exc:
        print(f"[s3-run-queues] declare failed (runs will wait for the re-drive scan): {exc}")

    # AI Mode rides the same connection with its OWN channel/queue (independent
    # QoS — scrape.do concurrency must not share SerpWow's prefetch). Best-effort:
    # a failure here 503s AI-Mode uploads (broker.is_ready() False) but must not
    # take SerpWow down with it.
    try:
        from app.services.ai_mode import broker as ai_mode_broker

        await ai_mode_broker.init_ai_mode_broker(rabbitmq_connection)
    except Exception as exc:
        print(f"[ai-mode-broker] init failed (SerpWow unaffected): {exc}")


async def close_rabbitmq() -> None:
    global rabbitmq_connection, rabbitmq_channel, rabbitmq_exchange, rabbitmq_queue, rabbitmq_consumer_tasks

    await stop_worker_consumers()

    try:
        from app.services.ai_mode import broker as ai_mode_broker

        await ai_mode_broker.close_ai_mode_broker()
    except Exception:
        pass

    rabbitmq_queue = None
    rabbitmq_exchange = None
    rabbitmq_channel = None

    if rabbitmq_connection is not None:
        await rabbitmq_connection.close()
        rabbitmq_connection = None


async def publish_job(job: dict[str, Any]) -> None:
    if rabbitmq_exchange is None:
        raise RuntimeError("RabbitMQ exchange is not initialized")

    routing_key = os.getenv("RABBITMQ_ROUTING_KEY", "singleRA.search.validate")
    message = aio_pika.Message(
        body=json.dumps(job).encode("utf-8"),
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        content_type="application/json",
    )
    # Wrap publish in asyncio.wait_for to prevent blocking indefinitely under RabbitMQ flow control/resource alarms
    await asyncio.wait_for(
        rabbitmq_exchange.publish(message, routing_key=routing_key),
        timeout=5.0
    )
    log_row_index: Optional[int] = None
    try:
        if job.get("row_index") is not None:
            log_row_index = int(job.get("row_index"))
    except Exception:
        log_row_index = None
    _log_row_stage(
        "producer.publish",
        f"routing_key={routing_key!r}",
        upload_id=str(job.get('upload_id') or "").strip() or None,
        row_index=log_row_index,
    )


async def publish_relationship_run(run_id: str) -> None:
    """Publish the ONE message that drives a whole relationship run.

    Goes to the shared named exchange under RELATIONSHIP_ROUTING_KEY — the same
    exchange consume_relationship_runs binds its queue to, and the one AI Mode and
    SerpWow already use, so every binding is visible in one place in the management UI.

    Failure is tolerated — and that tolerance lives HERE, not at the call sites, so every
    caller (upload, retry-failed-rows, anything added later) gets it. Both callers reach
    this only AFTER the run is fully created in S3, so letting a wait_for timeout or any
    AMQP error propagate 500s the request with no upload_id while the run exists and the
    re-drive scan starts spending money on it 300s later. Never raises: the worker's
    stale-run scan picks the run up within RELATIONSHIP_REDRIVE_SCAN_SEC, so a broker
    hiccup delays a run, never loses it.
    """
    from app.services.serpwow.relationship_runner import RELATIONSHIP_ROUTING_KEY

    if rabbitmq_exchange is None:
        print(f"[relationship] run {run_id} queued without a broker; "
              f"the re-drive scan will start it")
        return
    try:
        await asyncio.wait_for(
            rabbitmq_exchange.publish(
                aio_pika.Message(
                    body=json.dumps({"run_id": run_id}).encode("utf-8"),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    content_type="application/json",
                ),
                routing_key=RELATIONSHIP_ROUTING_KEY,
            ),
            timeout=5.0,
        )
    except Exception as exc:
        print(f"[relationship] publish failed for run {run_id} "
              f"({type(exc).__name__}: {exc}); the re-drive scan will start it")


async def publish_gmaps_run(run_id: str) -> None:
    """Publish the ONE message that drives a whole gmaps run.

    Same contract as publish_relationship_run, including that failure is tolerated HERE
    rather than at the call sites: the run already exists in S3 by this point, so letting
    an AMQP error propagate would 500 the request with no upload_id while the run exists.
    The worker's stale-run scan picks it up, so a broker hiccup delays a run, never loses
    one.
    """
    from app.services.serpwow.gmaps_runner import GMAPS_ROUTING_KEY

    if rabbitmq_exchange is None:
        print(f"[gmaps] run {run_id} queued without a broker; "
              f"the re-drive scan will start it")
        return
    try:
        await asyncio.wait_for(
            rabbitmq_exchange.publish(
                aio_pika.Message(
                    body=json.dumps({"run_id": run_id}).encode("utf-8"),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    content_type="application/json",
                ),
                routing_key=GMAPS_ROUTING_KEY,
            ),
            timeout=5.0,
        )
    except Exception as exc:
        print(f"[gmaps] publish failed for run {run_id} "
              f"({type(exc).__name__}: {exc}); the re-drive scan will start it")


async def publish_firmographics_run(run_id: str) -> None:
    """Publish the ONE message that drives a whole firmographics run.

    Same contract as publish_gmaps_run, including that failure is tolerated HERE rather
    than at the call site: the run already exists in S3 by this point, so letting an AMQP
    error propagate would 500 the request with no upload_id while the run exists. The
    worker's stale-run scan picks it up, so a broker hiccup delays a run, never loses one.
    """
    from app.services.serpwow.firmographics_runner import FIRMOGRAPHICS_ROUTING_KEY

    if rabbitmq_exchange is None:
        print(f"[firmographics] run {run_id} queued without a broker; "
              f"the re-drive scan will start it")
        return
    try:
        await asyncio.wait_for(
            rabbitmq_exchange.publish(
                aio_pika.Message(
                    body=json.dumps({"run_id": run_id}).encode("utf-8"),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    content_type="application/json",
                ),
                routing_key=FIRMOGRAPHICS_ROUTING_KEY,
            ),
            timeout=5.0,
        )
    except Exception as exc:
        print(f"[firmographics] publish failed for run {run_id} "
              f"({type(exc).__name__}: {exc}); the re-drive scan will start it")


def _finalize_row_outcome(result: dict[str, Any], *, pipeline: str, batch_postprocess_enabled: bool):
    """Return (OutcomeInfo, row_status). For out-of-scope pipelines, preserve the
    legacy binary: found->completed, everything-else->failed (no not_found remap)."""
    ctx = result.get("context") if isinstance(result.get("context"), dict) else {}
    info = _outcomes.classify_finalized_row(
        result, pipeline=pipeline,
        ctx_row_error=(str(ctx.get("row_error")).strip() or None) if ctx.get("row_error") else None,
        skip_llm=bool(ctx.get("skip_llm")))
    if pipeline in COST_SUMMARY_PIPELINES:
        # firmographics joined this set in 2026-08-19: a row whose website simply has no
        # AI overview is a business not-found, and the legacy binary below turned that
        # into a "failed" row that "Rerun failed" would re-buy at 10 credits a go.
        return info, info.row_status
    # Out of scope: legacy behavior — only 'found' is completed.
    return info, ("completed" if info.outcome == _outcomes.OUTCOME_FOUND else "failed")


async def process_upload_job(job: dict[str, Any]) -> None:
    upload_id = str(job["upload_id"])
    row_index = int(job["row_index"])
    company_name = str(job["company_name"])
    country = str(job["country"])
    firm_id = str(job.get("firm_id") or "") or None
    input_industry = str(job.get("industry") or "") or None
    input_full_address = str(job.get("full_address") or "") or None
    official_website_input = str(job.get("official_website") or "") or None
    pipeline = str(job.get("pipeline") or "").strip() or ""
    started_monotonic = asyncio.get_event_loop().time()
    _log_row_stage(
        "worker.row_start",
        (
            f"company={_short_text(company_name, 120)!r} "
            f"country={_short_text(country, 60)!r} "
            f"pipeline={pipeline!r} "
            f"has_industry={bool(input_industry)} has_full_address={bool(input_full_address)}"
        ),
        upload_id=upload_id,
        row_index=row_index,
    )
    try:
        # Idempotency guard: if this row is already terminal, skip duplicate/replayed messages.
        try:
            current_state = await get_upload_state(upload_id)
            current_row = next(
                (r for r in current_state.get("rows", []) if int(r.get("row_index", 0) or 0) == row_index),
                None,
            )
            if isinstance(current_row, dict) and current_row.get("status") in {"completed", "failed"}:
                _log_row_stage(
                    "worker.row_skip_duplicate",
                    f"existing_status={current_row.get('status')}",
                    upload_id=upload_id,
                    row_index=row_index,
                )
                return
        except Exception:
            # If we cannot read state, continue and let normal processing path handle failures.
            pass

        await update_row_state(upload_id, row_index, status="processing", error=None)
        _log_row_stage(
            "worker.row_mark_processing",
            "row state -> processing",
            upload_id=upload_id,
            row_index=row_index,
        )

        if pipeline == PIPELINE_FIRMOGRAPHICS:
            crawl_response, serpwow_raw_json = await execute_firmographic_extraction(
                official_website=str(official_website_input or ""),
                company_name=company_name,
                country=country,
                firm_id=firm_id,
                input_industry=input_industry,
                input_full_address=input_full_address,
            )
        elif pipeline == PIPELINE_GSEARCH:
            phase_value = str(job.get("phase") or "all").strip() or "all"
            crawl_response, serpwow_raw_json = await execute_gsearch_lookup_for_worker(
                company_name=company_name,
                country=country,
                firm_id=firm_id,
                input_industry=input_industry,
                input_full_address=input_full_address,
                debug_upload_id=upload_id,
                debug_row_index=row_index,
                phase=phase_value,
            )
        else:
            # relationship is deliberately absent: it is run-driven (one message per
            # RUN, see relationship_runner), never row-driven, so a relationship job
            # arriving here is a bug this correctly rejects.
            raise ValueError(f"unknown pipeline {pipeline!r}")

        raw_response_key, s3_error = await upload_raw_response_to_s3(
            upload_id=upload_id,
            row_index=row_index,
            raw_json=serpwow_raw_json,
            pipeline=pipeline,
            upload_company_name=str(job.get("upload_company_name") or company_name),
            row_company_name=company_name,
        )

        result = crawl_response.model_dump()
        result["raw_response_s3_key"] = raw_response_key
        result["raw_response_s3_error"] = s3_error
        # Record total per-row processing time so each pipeline's flow duration is visible
        # in the output/XLSX (parallels the AI-mode per-batch timing).
        if isinstance(result.get("context"), dict):
            # MERGE, don't replace: an executor may already have recorded its own phase
            # split in here (firmographics writes search_seconds/llm_seconds), and
            # assigning a fresh dict silently dropped it.
            timing = result["context"].get("timing")
            if not isinstance(timing, dict):
                timing = {}
            timing["total_seconds"] = round(
                asyncio.get_event_loop().time() - started_monotonic, 3)
            result["context"]["timing"] = timing

        official_website = (result.get("official_website") or "").strip()
        is_successful = bool(official_website)
        batch_postprocess_enabled = _batch_postprocess_enabled_for(pipeline)
        _row_ctx = result.get("context") if isinstance(result.get("context"), dict) else {}
        _llm_skipped = bool(_row_ctx.get("skip_llm"))
        info, row_status = _finalize_row_outcome(
            result, pipeline=pipeline, batch_postprocess_enabled=batch_postprocess_enabled)
        if (not is_successful) and batch_postprocess_enabled and not _llm_skipped:
            # Batch will decide later — keep the row non-terminal-error meanwhile.
            row_status = "completed"
            row_error = "Pending Gemini batch post-processing decision."
            info = _outcomes.OutcomeInfo(_outcomes.OUTCOME_NOT_FOUND)  # provisional
        else:
            row_error = info.error_detail if info.outcome == _outcomes.OUTCOME_ERROR else (
                None if info.outcome == _outcomes.OUTCOME_FOUND
                else (_row_ctx.get("row_error") or _outcomes.GENERIC_NOT_FOUND))

        await update_row_state(
            upload_id,
            row_index,
            status=row_status,
            error=row_error,
            result=result,
            raw_response_s3_key=raw_response_key,
            outcome=info.outcome,
            error_source=info.error_source,
            error_category=info.error_category,
            degraded_search=info.degraded_search,
        )
        elapsed_sec = asyncio.get_event_loop().time() - started_monotonic
        _log_row_stage(
            "worker.row_done",
            (
                f"status={row_status} "
                f"official={_short_text(official_website, 160)!r} "
                f"s3_key={_short_text(raw_response_key, 140)!r} "
                f"s3_error={_short_text(s3_error, 180)!r} "
                f"elapsed_sec={elapsed_sec:.2f}"
            ),
            upload_id=upload_id,
            row_index=row_index,
            level="INFO" if is_successful else "WARN",
        )
    except Exception as exc:
        info = _outcomes.classify_exception(exc, default_source=_outcomes.SRC_SERVER)
        try:
            await update_row_state(
                upload_id, row_index, status="failed", error=info.error_detail,
                outcome=info.outcome, error_source=info.error_source,
                error_category=info.error_category,
            )
        except Exception:
            # Preserve original exception for nack/requeue decision below.
            pass
        elapsed_sec = asyncio.get_event_loop().time() - started_monotonic
        _log_row_stage(
            "worker.row_exception",
            f"{type(exc).__name__}: {repr(exc)} elapsed_sec={elapsed_sec:.2f}",
            upload_id=upload_id,
            row_index=row_index,
            level="ERROR",
        )
        raise


async def rabbitmq_worker_loop(worker_id: int) -> None:
    """
    Each worker registers as a real Basic.Consume subscriber via queue.iterator().
    This makes RabbitMQ count it as a genuine consumer (visible in the management UI),
    unlike queue.get() which uses Basic.Get and is invisible as a consumer registration.
    """
    global rabbitmq_stop_event
    if rabbitmq_queue is None or rabbitmq_stop_event is None:
        return

    job_timeout_sec = max(10.0, _get_float_env("WORKER_JOB_TIMEOUT_SEC", 600.0))

    async with rabbitmq_queue.iterator() as queue_iter:
        async for message in queue_iter:
            if rabbitmq_stop_event.is_set():
                # Nack and requeue so another worker or restart can pick it up.
                await message.nack(requeue=True)
                break

            should_requeue = False
            should_drop = False
            payload: Optional[dict[str, Any]] = None
            tracking_upload_id: Optional[str] = None
            tracking_row_index: Optional[int] = None
            message_started_monotonic = asyncio.get_event_loop().time()

            try:
                payload = json.loads(message.body.decode("utf-8"))
                if isinstance(payload, dict):
                    tracking_upload_id = str(payload.get("upload_id") or "").strip() or None
                    row_raw = payload.get("row_index")
                    if tracking_upload_id is not None:
                        try:
                            tracking_row_index = int(row_raw)
                        except Exception:
                            tracking_row_index = None
                _log_row_stage(
                    "worker.message_received",
                    (
                        f"worker_id={worker_id} redelivered={bool(getattr(message, 'redelivered', False))} "
                        f"routing_key={_short_text(getattr(message, 'routing_key', None), 120)!r}"
                    ),
                    upload_id=tracking_upload_id,
                    row_index=tracking_row_index,
                )
                if tracking_upload_id is not None and tracking_row_index is not None:
                    await _mark_upload_row_active(tracking_upload_id, tracking_row_index)
                await asyncio.wait_for(process_upload_job(payload), timeout=job_timeout_sec)
                await message.ack()
                elapsed_sec = asyncio.get_event_loop().time() - message_started_monotonic
                _log_row_stage(
                    "worker.message_ack",
                    f"worker_id={worker_id} elapsed_sec={elapsed_sec:.2f}",
                    upload_id=tracking_upload_id,
                    row_index=tracking_row_index,
                )
            except asyncio.CancelledError:
                await message.nack(requeue=True)
                raise
            except Exception as exc:
                # One requeue attempt for transient issues; drop on redelivery to avoid poison loops.
                should_requeue = not bool(getattr(message, "redelivered", False))
                should_drop = not should_requeue
                disposition = "requeue" if should_requeue else "drop"
                print(
                    f"[worker:{worker_id}] job failed, will {disposition}: "
                    f"{type(exc).__name__}: {repr(exc)}"
                )
                _log_row_stage(
                    "worker.message_fail",
                    (
                        f"worker_id={worker_id} disposition={disposition} "
                        f"redelivered={bool(getattr(message, 'redelivered', False))} "
                        f"error={_short_text(repr(exc), 240)!r}"
                    ),
                    upload_id=tracking_upload_id,
                    row_index=tracking_row_index,
                    level="ERROR",
                )

                if should_drop and isinstance(payload, dict):
                    try:
                        await asyncio.wait_for(
                            update_row_state(
                                upload_id=str(payload.get("upload_id")),
                                row_index=int(payload.get("row_index")),
                                status="failed",
                                error=f"Dropped after redelivery failure: {str(exc)}",
                            ),
                            timeout=15.0,
                        )
                    except Exception as final_state_exc:
                        print(
                            f"[worker:{worker_id}] failed to persist terminal row status on drop: "
                            f"{type(final_state_exc).__name__}: {repr(final_state_exc)}"
                        )

                try:
                    if should_requeue:
                        await message.nack(requeue=True)
                        _log_row_stage(
                            "worker.message_nack_requeue",
                            f"worker_id={worker_id}",
                            upload_id=tracking_upload_id,
                            row_index=tracking_row_index,
                            level="WARN",
                        )
                    else:
                        await message.reject(requeue=False)
                        _log_row_stage(
                            "worker.message_reject_drop",
                            f"worker_id={worker_id}",
                            upload_id=tracking_upload_id,
                            row_index=tracking_row_index,
                            level="WARN",
                        )
                except ChannelInvalidStateError:
                    if rabbitmq_stop_event.is_set():
                        break
                except Exception:
                    if rabbitmq_stop_event.is_set():
                        break
            finally:
                if tracking_upload_id is not None and tracking_row_index is not None:
                    await _mark_upload_row_inactive(tracking_upload_id, tracking_row_index)


async def _collect_states_for_reconcile(limit: int) -> list[dict[str, Any]]:
    """Load upload state dicts from local disk, falling back to S3 cold-start."""
    states: list[dict[str, Any]] = []
    try:
        paths = await asyncio.to_thread(_list_local_state_files_sync, limit)
        for p in paths:
            try:
                states.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
    except Exception:
        pass
    if not states and os.getenv("S3_BUCKET"):
        try:
            keys = await asyncio.to_thread(_list_state_keys_from_s3_sync, limit)
            for k in keys:
                try:
                    states.append(await asyncio.to_thread(_read_json_from_s3_sync, k))
                except Exception:
                    continue
        except Exception:
            pass
    return states


async def reconcile_pending_gemini_batches() -> None:
    """Resume any upload whose Gemini batch isn't terminal yet (durability sweep).

    The remote batch + its results live on Google's side and ``job_name`` is
    persisted to state.json BEFORE polling, so recovery is just "re-poll":
    re-dispatch ``run_gemini_batch_for_upload`` (which re-polls an existing
    job_name, or creates the job if none yet). Runs in the worker process (where
    batches are normally kicked) to avoid racing the API's on-status-poll resume;
    ``gemini_batch_tasks`` + ``get_upload_lock`` guard against duplicates.
    Best-effort: per-upload and sweep-wide errors are swallowed, never raised.
    """
    try:
        limit = _get_int_env("GEMINI_BATCH_RECONCILE_SCAN_LIMIT", 500)
        states = await _collect_states_for_reconcile(limit)
        for state in states:
            if not isinstance(state, dict):
                continue
            upload_id = str(state.get("upload_id") or "").strip()
            if not upload_id:
                continue
            if not _batch_postprocess_enabled_for(str(state.get("pipeline") or "")):
                continue
            gb = state.get("gemini_batch")
            if not isinstance(gb, dict):
                continue
            chunks = gb.get("chunks") if isinstance(gb.get("chunks"), list) else []
            chunk_pending = any(isinstance(c, dict) and c.get("status") in {"queued", "running"} for c in chunks)
            if gb.get("status") not in {"queued", "running"} and not chunk_pending:
                continue
            existing = gemini_batch_tasks.get(upload_id)
            if existing is not None and not existing.done():
                continue
            print(f"[reconcile] resuming gemini batch for upload {upload_id} "
                  f"(status={gb.get('status')})")
            gemini_batch_tasks[upload_id] = asyncio.create_task(
                run_gemini_batch_for_upload(upload_id))
    except Exception as exc:
        print(f"[reconcile] sweep failed (ignored): {type(exc).__name__}: {exc}")


async def reconcile_stuck_gsearch_rows() -> None:
    """Always-on terminalization net for gsearch rows (worker process).

    Works purely off durable timestamps in state.json (NOT in-memory counts), so it
    behaves identically in embedded and split API+worker deployments. For each gsearch
    upload still non-terminal, any row stuck in queued/processing past
    GSEARCH_ROW_STALE_TIMEOUT_SEC is re-published up to GSEARCH_ROW_MAX_REQUEUE times,
    then FORCE-FAILED so the completion barrier can always resolve. Best-effort.

    gsearch only: it is the last pipeline with per-row messages and a Phase 1->2 barrier.
    """
    if rabbitmq_exchange is None or rabbitmq_queue is None:
        return
    try:
        from datetime import datetime, timezone
        stale_sec = max(60.0, _get_float_env("GSEARCH_ROW_STALE_TIMEOUT_SEC", 600.0))
        max_requeue = max(0, _get_int_env("GSEARCH_ROW_MAX_REQUEUE", 1))
        limit = _get_int_env("GEMINI_BATCH_RECONCILE_SCAN_LIMIT", 500)
        states = await _collect_states_for_reconcile(limit)
        # Only act when the queue is drained — avoids racing rows that are genuinely
        # in flight on a busy worker.
        queue_depth = await _get_rabbitmq_queue_depth()
        if queue_depth is None or queue_depth > 0:
            return
        now = datetime.now(timezone.utc)
        for state in states:
            if not isinstance(state, dict):
                continue
            _rec_pipe = str(state.get("pipeline") or "")
            # gsearch alone needs terminalization (its Phase 1->2 barrier). The S3-only
            # pipelines are not here: they have no per-row messages to re-publish and run
            # their own stale-run scans (relationship_runner / gmaps_runner).
            if _rec_pipe != PIPELINE_GSEARCH:
                continue
            if str(state.get("status") or "") not in {"queued", "processing"}:
                continue
            upload_id = str(state.get("upload_id") or "").strip()
            if not upload_id:
                continue
            stuck = []
            for row in state.get("rows", []) or []:
                if not isinstance(row, dict) or row.get("status") not in {"queued", "processing"}:
                    continue
                ts = _parse_iso_datetime(row.get("status_updated_at") or row.get("processing_started_at"))
                if ts is None or (now - ts).total_seconds() < stale_sec:
                    continue
                stuck.append(row)
            if not stuck:
                continue
            pipeline = str(state.get("pipeline") or "")
            phase = str(state.get("phase") or "all")
            upload_company_name = str(state.get("company_name") or "")
            async with get_upload_lock(upload_id):
                latest = await read_upload_artifact(upload_id, "state")
                by_index = {int(r.get("row_index", 0) or 0): r
                            for r in latest.get("rows", []) or [] if isinstance(r, dict)}
                requeued, failed = 0, 0
                for s in stuck:
                    row = by_index.get(int(s.get("row_index", 0) or 0))
                    if row is None or row.get("status") not in {"queued", "processing"}:
                        continue
                    attempts = int(row.get("requeue_attempts", 0) or 0)
                    if attempts < max_requeue:
                        row["requeue_attempts"] = attempts + 1
                        row["status"] = "queued"
                        row["status_updated_at"] = _now_iso()
                        row["processing_started_at"] = None
                        try:
                            await publish_job(_build_row_job_payload(upload_id, row, pipeline, phase, upload_company_name=upload_company_name))
                            requeued += 1
                        except Exception as exc:
                            row["status"] = "failed"
                            row["error"] = f"Queue recovery publish failed: {exc}"
                            row["outcome"] = _outcomes.OUTCOME_ERROR
                            row["error_source"] = _outcomes.SRC_SERVER
                            row["error_category"] = _outcomes.CAT_TIMEOUT
                            failed += 1
                    else:
                        row["status"] = "failed"
                        row["status_updated_at"] = _now_iso()
                        row["processing_started_at"] = None
                        row["error"] = (
                            f"Row terminalized by reconciler after {attempts} requeue(s) "
                            f"(stale > {int(stale_sec)}s)."
                        )
                        row["outcome"] = _outcomes.OUTCOME_ERROR
                        row["error_source"] = _outcomes.SRC_SERVER
                        row["error_category"] = _outcomes.CAT_TIMEOUT
                        failed += 1
                await persist_upload_state(upload_id, latest)
                _log_row_stage(
                    "gsearch.row_reconcile",
                    f"stuck={len(stuck)} requeued={requeued} force_failed={failed} "
                    f"queue_depth={queue_depth}",
                    upload_id=upload_id, row_index=None,
                    level="WARN" if failed else "INFO",
                )
    except Exception as exc:
        print(f"[gsearch.row_reconcile] sweep failed (ignored): {type(exc).__name__}: {exc}")


async def periodic_batch_reconciler() -> None:
    """Re-run the batch reconciler sweep every GEMINI_BATCH_RECONCILE_INTERVAL_SEC
    until the stop event is set."""
    interval = max(30.0, _get_float_env("GEMINI_BATCH_RECONCILE_INTERVAL_SEC", 300.0))
    stop = gemini_batch_reconciler_stop
    if stop is None:
        return
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break
        await reconcile_pending_gemini_batches()
        await reconcile_stuck_gsearch_rows()
        try:
            from app.services.ai_mode import worker as ai_mode_worker

            await ai_mode_worker.reconcile_ai_mode_runs()
        except Exception as exc:
            print(f"[ai-mode-reconcile] sweep failed: {exc}")


async def start_worker_consumers(worker_count: Optional[int] = None) -> None:
    global rabbitmq_consumer_tasks, rabbitmq_stop_event
    global gemini_batch_reconciler_task, gemini_batch_reconciler_stop
    if rabbitmq_queue is None:
        raise RuntimeError("RabbitMQ queue is not initialized")
    if rabbitmq_consumer_tasks:
        return

    rabbitmq_stop_event = asyncio.Event()

    # HANDOFF blocker #4. relationship_runner._poll_to_terminal does a BLOCKING sleep inside
    # asyncio.to_thread, so each in-flight Gemini shard holds one default-executor thread for
    # the job's whole (multi-hour) life. The default pool is min(32, cpu+4) -- SIX on a 2-vCPU
    # box -- so GEMINI_BATCH_MAX_INFLIGHT above that created nothing. Its own docstring
    # prescribes exactly this fix.
    #
    # Sized off max_inflight PLUS headroom, never max_inflight alone: this executor is shared
    # with every S3 write, CSV parse and _write_json in the worker, and starving those to make
    # room for pollers would trade one bottleneck for a worse one. Consumers-only path, since
    # the API process makes no provider calls.
    #
    # engine's own chunk driver and AI Mode do NOT need this (they await asyncio.sleep, and
    # poll all shards from a single thread, respectively) -- it is relationship that pays.
    executor_size = max(32, llm_batch.max_inflight() + 16)
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=executor_size, thread_name_prefix="worker"))
    print(f"[worker] default executor sized to {executor_size} threads "
          f"(GEMINI_BATCH_MAX_INFLIGHT={llm_batch.max_inflight()})")

    count = worker_count if worker_count is not None else _get_int_env("WORKER_CONCURRENCY", 4)
    count = max(1, int(count))
    rabbitmq_consumer_tasks = [
        asyncio.create_task(rabbitmq_worker_loop(i + 1))
        for i in range(count)
    ]

    # Durability: resume any in-flight Gemini batch now (catches a restart) and
    # keep sweeping periodically (catches a lost in-process task without restart).
    if gemini_batch_reconciler_task is None:
        await reconcile_pending_gemini_batches()
        gemini_batch_reconciler_stop = asyncio.Event()
        gemini_batch_reconciler_task = asyncio.create_task(periodic_batch_reconciler())

    # AI Mode consumers ride the same worker process (own channel/queue/QoS).
    # Best-effort: an AI-Mode failure must not take the SerpWow consumers down.
    try:
        from app.services.ai_mode import worker as ai_mode_worker

        await ai_mode_worker.start_ai_mode_consumers()
        # Startup sweep: re-dispatch dead finish tasks, republish lost batches,
        # and flip phantom-'running' runs left behind by a hard kill.
        await ai_mode_worker.reconcile_ai_mode_runs()
    except Exception as exc:
        print(f"[ai-mode-worker] consumers failed to start: {exc}")


async def stop_worker_consumers() -> None:
    global rabbitmq_consumer_tasks, rabbitmq_stop_event

    try:
        from app.services.ai_mode import worker as ai_mode_worker

        await ai_mode_worker.stop_ai_mode_consumers()
    except Exception:
        pass

    if not rabbitmq_consumer_tasks:
        return

    if rabbitmq_stop_event is not None:
        rabbitmq_stop_event.set()

    # Give loops time to exit naturally (avoids cancelling in-flight RPC).
    done, pending = await asyncio.wait(rabbitmq_consumer_tasks, timeout=2.0)
    for task in pending:
        task.cancel()
    for task in done:
        try:
            await task
        except asyncio.CancelledError:
            pass
    for task in rabbitmq_consumer_tasks:
        if task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
    rabbitmq_consumer_tasks = []
    rabbitmq_stop_event = None


@app.on_event("startup")
async def startup_event() -> None:
    global rabbitmq_consumer_tasks, rabbitmq_last_error, search_fetch_semaphore

    search_fetch_concurrency = max(1, _get_int_env("SEARCH_FETCH_CONCURRENCY", 2))
    search_fetch_semaphore = asyncio.Semaphore(search_fetch_concurrency)
    # run_serpwow_search lives in serpwow_client and reads its own module's
    # global — publish the semaphore there too (engine's copy kept for compat).
    serpwow_client.search_fetch_semaphore = search_fetch_semaphore

    try:
        await init_rabbitmq()
        # Keep API process producer-only by default; worker runs in separate process.
        if os.getenv("ENABLE_EMBEDDED_WORKER", "false").lower() == "true":
            await start_worker_consumers()
    except Exception as exc:
        rabbitmq_last_error = str(exc)
        rabbitmq_consumer_tasks = []


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global gemini_batch_reconciler_task, gemini_batch_reconciler_stop
    if gemini_batch_reconciler_stop is not None:
        gemini_batch_reconciler_stop.set()
    if gemini_batch_reconciler_task is not None:
        gemini_batch_reconciler_task.cancel()
        try:
            await gemini_batch_reconciler_task
        except (asyncio.CancelledError, Exception):
            pass
        gemini_batch_reconciler_task = None
        gemini_batch_reconciler_stop = None
    for task in list(gemini_batch_tasks.values()):
        task.cancel()
    if gemini_batch_tasks:
        await asyncio.gather(*gemini_batch_tasks.values(), return_exceptions=True)
        gemini_batch_tasks.clear()
    # Release the pooled scrape.do connections (gmaps' Maps client and firmographics'
    # Search client each hold their own pool).
    try:
        from app.services.serpwow import scrapedo_maps_client, scrapedo_search_client

        await scrapedo_maps_client.close_shared_client()
        await scrapedo_search_client.close_shared_client()
    except Exception:
        pass
    await close_rabbitmq()


@app.get("/", response_class=JSONResponse)
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "single-ra-isi"}


@app.get("/ui")
async def ui_page() -> RedirectResponse:
    """Legacy UI retired — redirect to the new console at /app."""
    return RedirectResponse(url="/app", status_code=307)



@app.get("/get-url/")
async def get_url(url: str) -> dict[str, str]:
    return {"message": f"Successfully received URL: {url}", "url": url}


@app.post("/crawl/firmographics", response_model=CrawlResponse)
async def crawl_firmographics_post(payload: FirmographicsRequest) -> CrawlResponse:
    response, _ = await execute_firmographic_extraction(
        official_website=payload.official_website,
        company_name=payload.company_name,
        country=payload.country,
        firm_id=payload.firm_id,
        input_industry=payload.industry,
        input_full_address=payload.full_address,
    )
    return response


async def _create_upload_with_rows(
    file: UploadFile,
    parsed_rows: list[dict[str, str]],
    pipeline: str,
    phase: str = "all",
    *,
    company_id: str,
    company_name: str = "",
    extra_state: Optional[dict[str, Any]] = None,
    run_total_rows: Optional[int] = None,
) -> dict[str, Any]:
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv file is supported.")

    from app.services.companies import get_company_service
    from app.core.supabase_client import get_supabase_config_error

    company_svc = get_company_service()
    if company_svc is None:
        detail = "Supabase not configured (set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)"
        error = get_supabase_config_error()
        if error:
            detail = f"{detail}: {error}"
        raise HTTPException(
            status_code=503,
            detail=detail,
        )
    try:
        company = await asyncio.to_thread(company_svc.get_company, company_id)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Supabase unreachable — check SUPABASE_URL / project status",
        ) from exc
    if company is None:
        raise HTTPException(
            status_code=400, detail="unknown company_id — create the company first"
        )

    if rabbitmq_exchange is None:
        detail = "RabbitMQ not connected. Try again shortly."
        if rabbitmq_last_error:
            detail = f"{detail} Last error: {rabbitmq_last_error}"
        raise HTTPException(status_code=503, detail=detail)

    upload_id = str(uuid.uuid4())
    _upload_dir(upload_id, company_name)  # create upload dir with company folder
    now_iso = _now_iso()
    _log_row_stage(
        "upload.create",
        (
            f"pipeline={pipeline} "
            f"phase={phase} "
            f"filename={_short_text(file.filename, 180)!r} "
            f"parsed_rows={len(parsed_rows)}"
        ),
        upload_id=upload_id,
        row_index=None,
    )
    # Best-effort Supabase run tracking: create_run never raises (returns None on
    # failure) and the upload proceeds untracked if Supabase is unhappy.
    run_db_id = await asyncio.to_thread(
        company_svc.create_run,
        company_id=company_id,
        pipeline=pipeline,
        run_ref=upload_id,
        total_rows=(run_total_rows if run_total_rows is not None else len(parsed_rows)),
    )
    state = {
        "upload_id": upload_id,
        "company_id": company_id,
        "company_name": company_name,
        "run_db_id": run_db_id,
        "pipeline": pipeline,
        "phase": phase,
        "created_at": now_iso,
        "updated_at": now_iso,
        "status": "queued",
        "total_rows": len(parsed_rows),
        "processed_rows": 0,
        "success_rows": 0,
        "failed_rows": 0,
        "rows": [
            {
                "row_index": int(row["row_index"]),
                "company_name": row["company_name"],
                "country": row["country"],
                "firm_id": (row.get("firm_id") or ""),
                "industry": (row.get("industry") or ""),
                "full_address": (row.get("full_address") or ""),
                "official_website": (row.get("official_website") or ""),
                "status": "queued",
                "status_updated_at": now_iso,
                "processing_started_at": None,
                "error": None,
                "raw_response_s3_key": None,
                "result": None,
            }
            for row in parsed_rows
        ],
    }
    if extra_state:
        state.update(extra_state)
    if _batch_postprocess_enabled_for(pipeline):
        state["gemini_batch"] = {
            "status": "waiting_for_rows",
            "queued_at": None,
            "job_name": None,
            "error": None,
        }

    async with get_upload_lock(upload_id):
        await persist_upload_state(upload_id, state)

    enqueued = 0
    enqueue_errors: list[dict[str, Any]] = []
    for row in parsed_rows:
        job = {
            "upload_id": upload_id,
            "row_index": int(row["row_index"]),
            "company_name": row["company_name"],
            "country": row["country"],
            "firm_id": (row.get("firm_id") or ""),
            "industry": (row.get("industry") or ""),
            "full_address": (row.get("full_address") or ""),
            "official_website": (row.get("official_website") or ""),
            "pipeline": pipeline,
            "phase": phase,
            "uploaded_at": _now_iso(),
            "upload_company_name": company_name,
        }
        try:
            await publish_job(job)
            enqueued += 1
        except Exception as exc:
            enqueue_errors.append(
                {
                    "row_index": int(row["row_index"]),
                    "company_name": row["company_name"],
                    "error": str(exc),
                }
            )
            await update_row_state(
                upload_id,
                int(row["row_index"]),
                status="failed",
                error=f"Queue publish failed: {str(exc)}",
            )
    _log_row_stage(
        "upload.enqueue_summary",
        f"total_rows={len(parsed_rows)} enqueued={enqueued} enqueue_errors={len(enqueue_errors)}",
        upload_id=upload_id,
        row_index=None,
        level="WARN" if enqueue_errors else "INFO",
    )

    return {
        "upload_id": upload_id,
        "filename": file.filename,
        "total_rows": len(parsed_rows),
        "enqueued_rows": enqueued,
        "enqueue_errors": enqueue_errors,
        "status_url": f"/uploads/{upload_id}/status",
        "output_url": f"/uploads/{upload_id}/output",
        "output_csv_url": f"/uploads/{upload_id}/output?format=csv",
        "output_xlsx_url": f"/uploads/{upload_id}/output?format=xlsx",
    }


@app.post("/uploads/firmographics")
async def create_firmographics_upload(
    file: UploadFile = File(...),
    company_id: str = Form(...),
    company_name: str = Form(""),
) -> dict[str, Any]:
    """Create an S3-only firmographics run: validate, park input.csv in S3, publish ONE
    message.

    No state.json and no local disk (2026-08-20). The rows are never materialised here —
    the worker streams input.csv back out of S3 — which is what makes a 500k-row upload
    cost the API process a fixed amount of memory, and what removes the ~2.7k-row ceiling
    the old per-row state.json rewrite imposed.
    """
    import uuid

    from app.core import s3 as core_s3
    from app.services.serpwow import s3_run_store as run_store
    from app.services.serpwow.csv_input import count_firmographics_csv_rows
    from app.services.serpwow.firmographics_runner import (
        PIPELINE_SEGMENT as FIRMO_SEGMENT,
    )

    raw = await file.read()
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv file is supported.")
    # CSV first: a bad CSV must report the CSV problem, not a config problem. Validates
    # every row and retains none — the count is all this endpoint needs.
    try:
        total = await asyncio.to_thread(count_firmographics_csv_rows, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not os.getenv("SCRAPEDO_TOKEN", "").strip():
        raise HTTPException(
            status_code=400,
            detail="SCRAPEDO_TOKEN is not configured — required for the firmographics "
                   "pipeline.")
    if not os.getenv("GEMINI_API_KEY", "").strip():
        raise HTTPException(
            status_code=400,
            detail="GEMINI_API_KEY is not configured — required to normalise the AI "
                   "overview into firmographic fields.")
    # S3 is not optional the way it is for the state-driven pipelines: this run has no
    # local disk and no state.json, so an unset bucket means the very next put_bytes
    # raises RuntimeError and the user gets an opaque 500 instead of this 400.
    if not core_s3.is_configured():
        raise HTTPException(
            status_code=400,
            detail="S3_BUCKET is not configured — required for the firmographics pipeline.")

    run_id = uuid.uuid4().hex
    prefix = run_store.run_prefix(company_name or company_id, run_id, FIRMO_SEGMENT)

    from app.services.companies import get_company_service

    svc = get_company_service()
    run_db_id = await asyncio.to_thread(
        svc.create_run, company_id=company_id, pipeline=PIPELINE_FIRMOGRAPHICS,
        run_ref=run_id, total_rows=total) if svc is not None else None

    await asyncio.to_thread(run_store.put_bytes, run_store.input_key(prefix), raw)
    # run_db_id rides in the pointer: there is no state dict to read it from later.
    await asyncio.to_thread(run_store.write_run_pointer, run_id, prefix,
                            company_name or company_id, run_db_id, FIRMO_SEGMENT)
    # The API writes status.json exactly once, before publishing; from the first scrape on
    # the worker is the only writer. created_at is stamped ONCE here and carried by every
    # later drive, so total wall clock survives a worker restart mid-run.
    counters = run_store.Counters(prefix, rows_total=total, phase="queued")
    await asyncio.to_thread(counters.flush, True)

    await publish_firmographics_run(run_id)
    return {"upload_id": run_id, "pipeline": PIPELINE_FIRMOGRAPHICS,
            "total_rows": total, "company_id": company_id}


@app.post("/uploads/gmaps")
async def create_gmaps_upload(
    file: UploadFile = File(...),
    company_id: str = Form(...),
    company_name: str = Form(""),
) -> dict[str, Any]:
    """Create an S3-only gmaps run: validate, park input.csv in S3, publish ONE message.

    No state.json and no local disk (2026-08). The rows are never materialised here — the
    worker streams input.csv back out of S3 — which is what makes a 500k-row upload cost
    the API process a fixed amount of memory instead of ~150MB of Entity objects.
    """
    import uuid

    from app.core import s3 as core_s3
    from app.models.entities import InvalidCSVError, parse_entities_csv
    from app.services.serpwow import s3_run_store as run_store
    from app.services.serpwow.gmaps_runner import PIPELINE_SEGMENT as GMAPS_SEGMENT

    raw = await file.read()
    # CSV first: a bad CSV must report the CSV problem, not a config problem.
    # sample_limit=0 validates every row but retains none — only the count is used.
    try:
        parsed = parse_entities_csv(raw, sample_limit=0)
    except InvalidCSVError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # gmaps runs on scrape.do's Google Maps API — fail fast instead of burning a whole
    # run's rows on a missing token. Checked AFTER CSV validation so a user with a bad
    # CSV hears about their CSV, not about our server config.
    if not os.getenv("SCRAPEDO_TOKEN", "").strip():
        raise HTTPException(
            status_code=400,
            detail="SCRAPEDO_TOKEN is not configured — required for the gmaps pipeline.")
    # S3 is not optional the way it is for the state-driven pipelines: this run has no
    # local disk and no state.json, so an unset bucket means the very next put_bytes
    # raises RuntimeError and the user gets an opaque 500 instead of this 400.
    if not core_s3.is_configured():
        raise HTTPException(
            status_code=400,
            detail="S3_BUCKET is not configured — required for the gmaps pipeline.")

    run_id = uuid.uuid4().hex
    prefix = run_store.run_prefix(company_name or company_id, run_id, GMAPS_SEGMENT)
    total = parsed.total_rows

    from app.services.companies import get_company_service

    svc = get_company_service()
    run_db_id = await asyncio.to_thread(
        svc.create_run, company_id=company_id, pipeline=PIPELINE_GMAPS,
        run_ref=run_id, total_rows=total) if svc is not None else None

    await asyncio.to_thread(run_store.put_bytes, run_store.input_key(prefix), raw)
    # run_db_id rides in the pointer: there is no state dict to read it from later.
    await asyncio.to_thread(run_store.write_run_pointer, run_id, prefix,
                            company_name or company_id, run_db_id, GMAPS_SEGMENT)
    # The API writes status.json exactly once, before publishing; from the first scrape on
    # the worker is the only writer. created_at is stamped ONCE here and carried by every
    # later drive, so total wall clock survives a worker restart mid-run.
    counters = run_store.Counters(prefix, rows_total=total, phase="queued")
    await asyncio.to_thread(counters.flush, True)

    await publish_gmaps_run(run_id)
    return {"upload_id": run_id, "pipeline": PIPELINE_GMAPS,
            "total_rows": total, "company_id": company_id}


@app.post("/uploads/gsearch")
async def create_gsearch_upload(
    file: UploadFile = File(...),
    phase: str = Form("all"),
    company_id: str = Form(...),
    company_name: str = Form(""),
) -> dict[str, Any]:
    raw = await file.read()
    _validate_canonical_upload_csv(raw)
    try:
        parsed_rows = parse_csv_rows(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _create_upload_with_rows(
        file, parsed_rows, PIPELINE_GSEARCH, phase=phase, company_id=company_id, company_name=company_name
    )


@app.post("/uploads/firmographics/preview")
async def preview_firmographics_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Dry-run parse for the New Run preview, using the parser the UPLOAD uses.

    The shared /uploads/preview runs parse_entities_csv, which requires company_name +
    country; a firmographics CSV has neither, so it fell through to positional parsing
    and told the user "col 1 = company name, col 2 = country" about a file whose first
    column is a URL. Costs nothing — no state, no queue, no Supabase.
    """
    from app.services.serpwow.csv_input import firmographics_columns

    raw = await file.read()
    try:
        rows = parse_firmographics_csv_rows(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    header = next(csv.reader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))), [])
    sample_columns = ["website_url", "company_name", "country",
                      "firm_id", "industry", "full_address"]
    return {
        "total_rows": len(rows),
        "positional": False,
        "warnings": [] if rows else ["No enrichable rows — every row is missing a website."],
        "columns_detected": firmographics_columns(header),
        "sample_columns": sample_columns,
        # The row dict's key is still official_website (the state/executor field name);
        # only the user-facing column is website_url.
        "sample_rows": [{**{c: r.get(c, "") for c in sample_columns},
                         "website_url": r["official_website"]}
                        for r in rows[:5]],
    }


@app.post("/uploads/relationship/preview")
async def preview_relationship_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Dry-run parse for the New Run preview: header mapping, row count, and a
    sample of the rows that would be searched (one row in → one row out, no dedup).
    Costs nothing — no state, no queue, no Supabase."""
    from app.services.serpwow.relationship_csv import (
        InvalidRelationshipCSV,
        parse_relationship_csv,
    )

    raw = await file.read()
    try:
        parsed = parse_relationship_csv(raw)
    except InvalidRelationshipCSV as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = parsed["rows"]          # first SAMPLE_LIMIT only, never the whole file
    total = parsed["total_rows"]
    warnings: list[str] = []
    if not rows:
        warnings.append("No searchable rows — the CSV contains no data rows.")

    return {
        "total_rows": total,
        "relationship": True,
        "warnings": warnings,
        "columns_detected": parsed["columns_detected"],
        "sample_columns": ["company_name_x", "company_name_y", "input_url"],
        "sample_rows": [
            {
                "company_name_x": r["x_name"],
                "company_name_y": r["y_name"],
                "input_url": r["input_url"],
            }
            for r in rows
        ],
    }


@app.post("/uploads/relationship")
async def create_relationship_upload(
    file: UploadFile = File(...),
    company_id: str = Form(...),
    company_name: str = Form(""),
) -> dict[str, Any]:
    import uuid

    from app.core import s3 as core_s3
    from app.services.serpwow import s3_run_store as rel_store
    from app.services.serpwow.relationship_csv import (
        InvalidRelationshipCSV,
        parse_relationship_csv,
    )

    raw = await file.read()
    # CSV first: a bad CSV must report the CSV problem, not a config problem.
    try:
        # sample_limit=0: the upload path needs the row COUNT and the raw bytes, which
        # go straight to S3 for the worker to stream. It never looks at parsed rows.
        parsed = parse_relationship_csv(raw, sample_limit=0)
    except InvalidRelationshipCSV as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # No empty-rows guard: parse_relationship_csv raises on a blank required value and
    # on a header-only CSV, so total_rows is never 0 when it returns.
    for env_key in ("GEMINI_API_KEY", "SCRAPEDO_TOKEN"):
        if not os.getenv(env_key, "").strip():
            raise HTTPException(
                status_code=400,
                detail=f"{env_key} is not configured — required for the relationship pipeline.")
    # S3 is not optional here the way it is for the state-driven pipelines: this run has
    # no local disk and no state.json, so an unset bucket means the very next put_bytes
    # raises RuntimeError and the user gets an opaque 500 instead of this 400.
    if not core_s3.is_configured():
        raise HTTPException(
            status_code=400,
            detail="S3_BUCKET is not configured — required for the relationship pipeline.")

    run_id = uuid.uuid4().hex
    prefix = rel_store.run_prefix(company_name or company_id, run_id)
    total = parsed["total_rows"]

    # Best-effort Supabase run row; create_run never raises (returns None untracked).
    # get_company_service() itself is None when Supabase is unconfigured.
    from app.services.companies import get_company_service

    svc = get_company_service()
    run_db_id = await asyncio.to_thread(
        svc.create_run, company_id=company_id, pipeline=PIPELINE_RELATIONSHIP,
        run_ref=run_id, total_rows=total) if svc is not None else None

    await asyncio.to_thread(rel_store.put_bytes, rel_store.input_key(prefix), raw)
    # run_db_id rides in the pointer: phase 3 has no state dict to read it from.
    await asyncio.to_thread(rel_store.write_run_pointer, run_id, prefix,
                            company_name or company_id, run_db_id)
    # The API writes status.json exactly once, before publishing. From the first scrape
    # on, the worker is the only writer.
    # created_at is stamped ONCE here and carried by every later drive, so total wall
    # clock survives a worker restart mid-run.
    counters = rel_store.Counters(prefix, rows_total=total, phase="queued")
    await asyncio.to_thread(counters.flush, True)

    await publish_relationship_run(run_id)
    return {"upload_id": run_id, "pipeline": PIPELINE_RELATIONSHIP,
            "total_rows": total, "company_id": company_id}


def _find_s3_run(run_id: str) -> tuple[Optional[dict[str, Any]], str]:
    """(pointer, segment) for an S3-only run, or (None, "") if this id is not one.

    THREE pipelines now keep their runs entirely in S3 with no state.json, each in its own
    pointer namespace. Endpoints that serve both — status, stop, retry, failure-analysis,
    result files — resolve the id here rather than each guessing a namespace. Two small
    GETs at most, and the miss is what tells the caller to fall through to the
    state-driven path.
    """
    from app.services.serpwow import s3_run_store as run_store
    from app.services.serpwow.gmaps_runner import PIPELINE_SEGMENT as GMAPS_SEGMENT
    from app.services.serpwow.firmographics_runner import (
        PIPELINE_SEGMENT as FIRMO_SEGMENT,
    )

    for segment in (run_store.PIPELINE_SEGMENT, GMAPS_SEGMENT, FIRMO_SEGMENT):
        pointer = run_store.read_run_pointer(run_id, segment)
        if pointer:
            return pointer, segment
    return None, ""


@app.post("/uploads/{upload_id}/retry-failed-rows")
async def retry_failed_rows(
    upload_id: str,
    limit: int = Query(0, ge=0, le=5000),
) -> dict[str, Any]:
    from app.services.serpwow import s3_run_store as rel_store

    # Rerun failed (S3-only pipelines): drop the error markers so a re-drive rescrapes
    # exactly those rows. Checked before the broker guard on purpose — the re-drive
    # scan starts the run even if the publish below is skipped.
    pointer, segment = await asyncio.to_thread(_find_s3_run, upload_id)
    if pointer:
        prefix = pointer["prefix"]

        def _dead_markers() -> list[str]:
            keys = list(rel_store.iter_keys(f"{prefix}/errors/"))
            # `limit` used to be accepted and then ignored, so a cautious "retry 100 of
            # them" silently tried all 50k.
            return keys[:limit] if limit else keys

        # Bulk delete: 1000 keys per call instead of one call per row, which is what made
        # this time out on any run with a lot of dead rows.
        removed = await asyncio.to_thread(
            lambda: rel_store.delete_objects(_dead_markers()))

        # Rows that scraped fine but never got a verdict, because their Gemini shard died.
        # They have NO error marker, so `removed` cannot see them — and a run whose only
        # problem was a dead shard therefore reported "Enqueued 0 failed rows" while
        # correctly republishing and redoing exactly this work. Counted, not deleted:
        # their pending_llm/ marker is what the LLM phase reads to find them, and their
        # scrape objects are what make the rerun free on the provider side.
        def _unjudged() -> int:
            # Which rows OWE an LLM result differs by pipeline, so ask each in its own
            # terms rather than assuming one marker:
            #   firmographics — only DEFERRED rows do (inline mode marks none), so it is
            #                   the pending_llm/ markers. Cheap: one LIST, and empty in
            #                   inline mode.
            #   relationship  — every scraped row does; its Gemini verdict IS the answer
            #                   and it has no inline path, so there are no markers to read.
            #   gmaps         — has no LLM at all.
            if segment == "gmaps":
                return 0
            owed = (rel_store.list_pending_llm_rows(prefix) if segment == "firmographics"
                    else rel_store.list_done_rows(prefix))
            return len(owed - rel_store.list_cleaned_rows(prefix))

        pending_llm = await asyncio.to_thread(_unjudged)
        await asyncio.to_thread(rel_store.clear_stop, prefix)
        publishers = {"gmaps": publish_gmaps_run,
                      "firmographics": publish_firmographics_run}
        await publishers.get(segment, publish_relationship_run)(upload_id)
        return {"upload_id": upload_id, "retried_rows": removed,
                "pending_llm_rows": pending_llm,
                # operations.js reads enqueued_rows for its status line. Both kinds of
                # work count: a dead row to re-scrape and an unjudged row to re-send to
                # Gemini are both things this request just put back in flight.
                "enqueued_rows": removed + pending_llm,
                "status_url": f"/uploads/{upload_id}/status"}

    if rabbitmq_exchange is None:
        detail = "RabbitMQ not connected. Try again shortly."
        if rabbitmq_last_error:
            detail = f"{detail} Last error: {rabbitmq_last_error}"
        raise HTTPException(status_code=503, detail=detail)

    try:
        current_state = await get_upload_state(upload_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload ID not found") from exc

    # A user-deleted batch may still have a local driver unwinding. Explicit
    # retry is the only operation allowed to clear the tombstone, so wait for
    # that older generation to exit before opening a new one.
    if _batch_deleted_by_user(current_state) or bool(
        current_state.get("batch_deleted_job_names")
    ):
        old_batch_task = gemini_batch_tasks.get(upload_id)
        if old_batch_task is not None and old_batch_task is not asyncio.current_task():
            if not old_batch_task.done():
                old_batch_task.cancel()
            await asyncio.gather(old_batch_task, return_exceptions=True)
            if gemini_batch_tasks.get(upload_id) is old_batch_task:
                gemini_batch_tasks.pop(upload_id, None)

    jobs_to_retry: list[dict[str, Any]] = []
    retried_row_indexes: list[int] = []

    async with get_upload_lock(upload_id):
        try:
            state = await read_upload_artifact(upload_id, "state")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Upload ID not found") from exc
        except Exception as exc:
            if "NoSuchKey" in str(exc):
                raise HTTPException(status_code=404, detail="Upload ID not found") from exc
            raise

        pipeline = str(state.get("pipeline") or "")
        phase = str(state.get("phase") or "all")
        # Retrying a stopped upload re-opens it: clear the stop marker so the
        # batch post-process can run again once the retried rows finish.
        retry_generation = _batch_generation(state)
        if _batch_postprocess_enabled_for(pipeline):
            clear_marker = await _clear_batch_deletion_tombstone(upload_id, state)
            retry_generation = (
                int(clear_marker.get("generation") or 0)
                if isinstance(clear_marker, dict)
                else retry_generation + 1
            )
        state.pop("stopped_by_user_at", None)
        state.pop("batch_deleted_by_user_at", None)
        state.pop("batch_deleted_job_names", None)
        failed_rows = [
            row for row in (state.get("rows") or [])
            if isinstance(row, dict) and row.get("status") in {"failed", "queued", "processing"}
        ]
        failed_rows.sort(key=lambda row: int(row.get("row_index", 0) or 0))
        if limit > 0:
            failed_rows = failed_rows[:limit]

        for row in failed_rows:
            row_index = int(row.get("row_index", 0) or 0)
            row["status"] = "queued"
            row["status_updated_at"] = _now_iso()
            row["processing_started_at"] = None
            row["error"] = None
            row["result"] = None
            row["raw_response_s3_key"] = None
            retried_row_indexes.append(row_index)
            jobs_to_retry.append(
                {
                    "upload_id": upload_id,
                    "row_index": row_index,
                    "company_name": str(row.get("company_name") or ""),
                    "country": str(row.get("country") or ""),
                    "firm_id": str(row.get("firm_id") or ""),
                    "industry": str(row.get("industry") or ""),
                    "full_address": str(row.get("full_address") or ""),
                    "official_website": str(row.get("official_website") or ""),
                    "pipeline": pipeline,
                    "phase": phase,
                    "uploaded_at": _now_iso(),
                    "upload_company_name": str(state.get("company_name") or ""),
                    **{k: row[k] for k in ("x_name", "input_url", "city") if k in row},
                }
            )

        if _batch_postprocess_enabled_for(pipeline):
            state["gemini_batch"] = {
                "status": "waiting_for_rows",
                "generation": retry_generation,
                "queued_at": None,
                "job_name": None,
                "error": None,
            }

        await persist_upload_state(upload_id, state)

    enqueued = 0
    enqueue_errors: list[dict[str, Any]] = []
    for job in jobs_to_retry:
        try:
            await publish_job(job)
            enqueued += 1
        except Exception as exc:
            enqueue_errors.append(
                {
                    "row_index": int(job["row_index"]),
                    "company_name": job["company_name"],
                    "error": str(exc),
                }
            )
            await update_row_state(
                upload_id,
                int(job["row_index"]),
                status="failed",
                error=f"Queue publish failed: {str(exc)}",
            )

    _log_row_stage(
        "upload.retry_failed_rows",
        (
            f"requested={len(retried_row_indexes)} "
            f"enqueued={enqueued} enqueue_errors={len(enqueue_errors)} "
            f"limit={limit}"
        ),
        upload_id=upload_id,
        row_index=None,
        level="WARN" if enqueue_errors else "INFO",
    )

    return {
        "upload_id": upload_id,
        "requested_retry_rows": len(retried_row_indexes),
        "enqueued_rows": enqueued,
        "enqueue_errors": enqueue_errors,
        "retry_row_indexes": retried_row_indexes,
        "status_url": f"/uploads/{upload_id}/status",
        "failure_analysis_url": f"/uploads/{upload_id}/failure-analysis",
    }


@app.post("/uploads/{upload_id}/stop")
async def stop_upload(upload_id: str) -> dict[str, Any]:
    """Stop a running SerpWow upload: mark all non-terminal rows failed (queued
    RabbitMQ messages are then dropped by the worker's idempotency guard) and
    best-effort cancel a pending/running Gemini batch. The upload terminalizes
    on this persist, so outputs/Supabase/Slack fire with whatever was done."""
    from app.services.serpwow import s3_run_store as rel_store

    # S3-only runs have no rows and no engine-side Gemini batch to cancel — the stop is
    # an S3 marker the runner polls between rows.
    pointer, _segment = await asyncio.to_thread(_find_s3_run, upload_id)
    if pointer:
        await asyncio.to_thread(rel_store.request_stop, pointer["prefix"])
        # run_detail.js reports stopped_rows/batch_cancelled in its confirmation
        # message; neither is knowable here (rows are not tracked individually), so
        # send honest zeros rather than leave the UI rendering "undefined".
        return {"upload_id": upload_id, "stop_requested": True,
                "stopped_rows": 0, "batch_cancelled": False,
                "status_url": f"/uploads/{upload_id}/status"}

    _pending_sentinel = "Pending Gemini batch post-processing decision."
    async with get_upload_lock(upload_id):
        try:
            state = await read_upload_artifact(upload_id, "state")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Upload ID not found") from exc
        except Exception as exc:
            if "NoSuchKey" in str(exc):
                raise HTTPException(status_code=404, detail="Upload ID not found") from exc
            raise

        gb = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
        batch_active = gb.get("status") in {"waiting_for_rows", "queued", "running"}
        rows_active = any(
            isinstance(r, dict) and r.get("status") in {"queued", "processing"}
            for r in state.get("rows") or []
        )
        if not rows_active and not batch_active:
            raise HTTPException(status_code=409, detail="Upload is already terminal — nothing to stop.")

        now = _now_iso()
        stopped_rows = 0
        for row in state.get("rows") or []:
            if not isinstance(row, dict):
                continue
            if row.get("status") in {"queued", "processing"}:
                row["status"] = "failed"
                row["error"] = "Stopped by user."
                row["status_updated_at"] = now
                stopped_rows += 1
            elif (row.get("status") == "completed"
                    and row.get("error") == _pending_sentinel
                    and not str((row.get("result") or {}).get("official_website") or "").strip()):
                # Row was parked waiting for the (now-cancelled) batch decision.
                row["status"] = "failed"
                row["error"] = "Stopped by user before Gemini batch decision."
                row["status_updated_at"] = now
                stopped_rows += 1

        batch_cancelled = False
        if batch_active:
            job_names = [str(c.get("job_name") or "") for c in (gb.get("chunks") or [])
                         if isinstance(c, dict) and c.get("job_name")]
            legacy_job = str(gb.get("job_name") or "").strip()
            if legacy_job:
                job_names.append(legacy_job)
            for job_name in job_names:
                try:
                    await asyncio.to_thread(_gemini_batch_cancel_sync, job_name)
                except Exception as exc:
                    _log_gemini_batch(upload_id, f"stop: cancel {job_name} failed (non-fatal): {exc!r}")
            state["gemini_batch"] = {**gb, "status": "failed", "completed_at": now,
                                     "error": "Stopped by user."}
            batch_cancelled = True

        # Marker consulted by maybe_start_gemini_batch_for_upload so the terminal
        # persist below doesn't immediately (re)start a batch for a stopped run.
        state["stopped_by_user_at"] = now
        await persist_upload_state(upload_id, state)
        final_status = str(state.get("status") or "")

    _log_row_stage(
        "upload.stop",
        f"stopped_rows={stopped_rows} batch_cancelled={batch_cancelled} final_status={final_status}",
        upload_id=upload_id,
        row_index=None,
        level="WARN",
    )
    return {
        "upload_id": upload_id,
        "stopped_rows": stopped_rows,
        "batch_cancelled": batch_cancelled,
        "status": final_status,
        "status_url": f"/uploads/{upload_id}/status",
    }


@app.get("/uploads")
async def uploads_list(
    limit: int = Query(100, ge=1, le=500),
    pipeline: Optional[str] = Query(None),
) -> dict[str, Any]:
    pipeline_value = None
    if pipeline:
        candidate = str(pipeline).strip().lower()
        if candidate in {PIPELINE_FIRMOGRAPHICS, PIPELINE_GMAPS, PIPELINE_GSEARCH, PIPELINE_RELATIONSHIP}:
            pipeline_value = candidate
    items = await list_upload_summaries(limit, pipeline=pipeline_value)
    return {"count": len(items), "uploads": items}


@app.get("/batch/jobs")
async def batch_jobs_list(limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    global gemini_batch_list_cache, gemini_batch_list_cache_fetched_at, gemini_batch_list_error_cooldown_until
    items = await list_upload_summaries(max(limit, 500), pipeline=None)
    local_by_job: dict[str, dict[str, Any]] = {}
    local_by_upload: dict[str, dict[str, Any]] = {}
    for item in items:
        if str(item.get("pipeline") or "") not in {
            PIPELINE_GSEARCH, PIPELINE_GMAPS, PIPELINE_RELATIONSHIP,
        }:
            continue
        batch_meta = item.get("gemini_batch") if isinstance(item.get("gemini_batch"), dict) else {}
        upload_id = str(item.get("upload_id") or "").strip()
        if upload_id:
            local_by_upload[upload_id] = {"item": item, "batch": batch_meta}
        job_name = str(batch_meta.get("job_name") or "").strip()
        if job_name:
            local_by_job[job_name] = {
                "item": item,
                "batch": batch_meta,
                "local_status": batch_meta.get("status"),
                "chunk": None,
            }
        chunks = batch_meta.get("chunks") if isinstance(batch_meta.get("chunks"), list) else []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            chunk_job_name = str(chunk.get("job_name") or "").strip()
            if chunk_job_name:
                local_by_job[chunk_job_name] = {
                    "item": item,
                    "batch": batch_meta,
                    "local_status": chunk.get("status"),
                    "chunk": chunk,
                }

    remote_error: Optional[str] = None
    operations: list[dict[str, Any]] = []
    now_mono = asyncio.get_event_loop().time()
    use_remote_list = _get_bool_env("ENABLE_REMOTE_GEMINI_BATCH_LIST", True)
    cache_ttl_sec = max(1.0, _get_float_env("GEMINI_BATCH_LIST_CACHE_TTL_SEC", 10.0))
    error_cooldown_sec = max(5.0, _get_float_env("GEMINI_BATCH_LIST_ERROR_COOLDOWN_SEC", 30.0))
    list_timeout_sec = max(1.0, _get_float_env("GEMINI_BATCH_LIST_TIMEOUT_SEC", 10.0))

    if use_remote_list:
        cache_fresh = (
            bool(gemini_batch_list_cache)
            and (now_mono - gemini_batch_list_cache_fetched_at) < cache_ttl_sec
        )
        if cache_fresh:
            operations = list(gemini_batch_list_cache)
        elif now_mono < gemini_batch_list_error_cooldown_until:
            operations = list(gemini_batch_list_cache)
            remaining = int(max(0.0, gemini_batch_list_error_cooldown_until - now_mono))
            remote_error = f"remote list temporarily paused after timeout/error (cooldown {remaining}s)"
        else:
            try:
                api_data = await asyncio.to_thread(_gemini_batch_list_sync, limit, list_timeout_sec)
                operations = api_data.get("operations") if isinstance(api_data, dict) else []
                if isinstance(operations, list):
                    gemini_batch_list_cache = [op for op in operations if isinstance(op, dict)]
                    gemini_batch_list_cache_fetched_at = now_mono
                gemini_batch_list_error_cooldown_until = 0.0
            except Exception as exc:
                operations = list(gemini_batch_list_cache)
                remote_error = str(exc)
                gemini_batch_list_error_cooldown_until = now_mono + error_cooldown_sec
                print(f"[batch-manager] remote Gemini list failed, falling back to local jobs: {remote_error}")

    jobs: list[dict[str, Any]] = []
    seen_job_names: set[str] = set()
    for op in operations if isinstance(operations, list) else []:
        if not isinstance(op, dict):
            continue
        job_name = str(op.get("name") or "").strip()
        if not job_name:
            continue
        live_state = _gemini_batch_state_name(op)
        done_flag = bool(op.get("done"))
        metadata = op.get("metadata") if isinstance(op.get("metadata"), dict) else {}
        error_obj = op.get("error")

        # Keep likely batch/LRO items; skip non-batch operations when identifiable.
        if not (
            live_state.startswith("BATCH_STATE_")
            or live_state.startswith("JOB_STATE_")
            or "batch" in job_name.lower()
            or "state" in metadata
        ):
            continue

        local_ref = local_by_job.get(job_name) or {}
        operation_matches_local_job = bool(local_ref)
        inferred_upload_id = _extract_upload_id_from_batch_obj(op)
        operation_generation = _extract_generation_from_batch_obj(op)
        if not local_ref and inferred_upload_id:
            local_ref = local_by_upload.get(inferred_upload_id) or {}
        local_item = local_ref.get("item") if isinstance(local_ref.get("item"), dict) else {}
        local_batch = local_ref.get("batch") if isinstance(local_ref.get("batch"), dict) else {}
        local_chunk = local_ref.get("chunk") if isinstance(local_ref.get("chunk"), dict) else {}

        batch_status = _derive_ui_batch_status(
            live_state=live_state,
            done_flag=done_flag,
            error_obj=error_obj,
            local_status=local_ref.get("local_status"),
        )

        jobs.append(
            {
                "upload_id": local_item.get("upload_id"),
                "batch_generation": (
                    _batch_generation({"gemini_batch": local_batch})
                    if operation_matches_local_job and local_batch
                    else operation_generation
                ),
                "chunk_id": local_chunk.get("chunk_id"),
                "upload_status": local_item.get("status"),
                "batch_status": batch_status,
                "job_name": job_name,
                "live_state": live_state,
                "done": done_flag,
                "updated_at": (
                    metadata.get("updateTime")
                    or metadata.get("endTime")
                    or local_item.get("updated_at")
                ),
                "started_at": metadata.get("createTime") or local_chunk.get("started_at") or local_batch.get("started_at"),
                "completed_at": metadata.get("endTime") or local_chunk.get("completed_at") or local_batch.get("completed_at"),
                "error": local_chunk.get("error") or local_batch.get("error") or (json.dumps(error_obj, ensure_ascii=True) if error_obj else None),
            }
        )
        seen_job_names.add(job_name)
        if not jobs[-1].get("upload_id") and inferred_upload_id:
            jobs[-1]["upload_id"] = inferred_upload_id

    # Always merge in locally tracked jobs that may not appear in the
    # current remote page (or may be temporarily absent from remote listing).
    for job_name, local_ref in local_by_job.items():
        if job_name in seen_job_names:
            continue
        local_item = local_ref.get("item") if isinstance(local_ref.get("item"), dict) else {}
        local_batch = local_ref.get("batch") if isinstance(local_ref.get("batch"), dict) else {}
        local_chunk = local_ref.get("chunk") if isinstance(local_ref.get("chunk"), dict) else {}
        jobs.append(
            {
                "upload_id": local_item.get("upload_id"),
                "batch_generation": (
                    _batch_generation({"gemini_batch": local_batch})
                    if local_batch else None
                ),
                "chunk_id": local_chunk.get("chunk_id"),
                "upload_status": local_item.get("status"),
                "batch_status": _derive_ui_batch_status(
                    live_state="",
                    done_flag=False,
                    error_obj=None,
                    local_status=local_ref.get("local_status"),
                ),
                "job_name": job_name,
                "live_state": None,
                "done": None,
                "updated_at": local_item.get("updated_at"),
                "started_at": local_chunk.get("started_at") or local_batch.get("started_at"),
                "completed_at": local_chunk.get("completed_at") or local_batch.get("completed_at"),
                "error": local_chunk.get("error") or local_batch.get("error"),
            }
        )

    jobs.sort(key=lambda item: str(item.get("updated_at") or item.get("started_at") or ""), reverse=True)
    return {
        "count": len(jobs),
        "jobs": jobs,
        "source": "remote" if remote_error is None else "local_fallback",
        "remote_error": remote_error,
    }


def _invalidate_gemini_batch_list_cache() -> None:
    global gemini_batch_list_cache, gemini_batch_list_cache_fetched_at, gemini_batch_list_error_cooldown_until
    gemini_batch_list_cache = []
    gemini_batch_list_cache_fetched_at = 0.0
    gemini_batch_list_error_cooldown_until = 0.0


@app.post("/batch/jobs/{upload_id}/status")
async def batch_job_get_status(upload_id: str) -> dict[str, Any]:
    try:
        state = await get_upload_state(upload_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload ID not found") from exc

    gemini_batch_meta = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
    job_name = str(gemini_batch_meta.get("job_name") or "").strip()
    if not job_name:
        raise HTTPException(status_code=400, detail="No Gemini batch job found for upload")

    try:
        batch_obj = await asyncio.to_thread(_gemini_batch_get_sync, job_name)
        live_state = _gemini_batch_state_name(batch_obj)
        done_flag = bool(batch_obj.get("done"))
        error_obj = batch_obj.get("error")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini batch status fetch failed: {str(exc)}") from exc

    batch_status = _derive_ui_batch_status(
        live_state=live_state,
        done_flag=done_flag,
        error_obj=error_obj,
        local_status=gemini_batch_meta.get("status"),
    )

    if batch_status != str(gemini_batch_meta.get("status") or ""):
        async with get_upload_lock(upload_id):
            try:
                latest = await read_upload_artifact(upload_id, "state")
                latest_batch = latest.get("gemini_batch") if isinstance(latest.get("gemini_batch"), dict) else {}
                latest_batch["status"] = batch_status
                latest_batch["job_name"] = job_name
                if done_flag:
                    latest_batch["completed_at"] = _now_iso()
                if error_obj:
                    latest_batch["error"] = json.dumps(error_obj, ensure_ascii=True)
                elif batch_status in {"succeeded", "running", "cancelled"}:
                    latest_batch["error"] = None
                latest["gemini_batch"] = latest_batch
                await persist_upload_state(upload_id, latest)
            except Exception:
                pass

    return {
        "upload_id": upload_id,
        "job_name": job_name,
        "batch_status": batch_status,
        "live_state": live_state,
        "done": done_flag,
        "raw": batch_obj,
    }


@app.post("/batch/jobs/{upload_id}/cancel")
async def batch_job_cancel(upload_id: str) -> dict[str, Any]:
    try:
        state = await get_upload_state(upload_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload ID not found") from exc

    gemini_batch_meta = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}

    # Chunked run: gather job_names from chunks
    chunks = gemini_batch_meta.get("chunks") if isinstance(gemini_batch_meta.get("chunks"), list) else []
    chunk_job_names = [str(c.get("job_name") or "").strip() for c in chunks if isinstance(c, dict)]
    chunk_job_names = [n for n in chunk_job_names if n]

    # Legacy single-job fallback
    top_level_job_name = str(gemini_batch_meta.get("job_name") or "").strip()

    if not chunk_job_names and not top_level_job_name:
        raise HTTPException(status_code=400, detail="No Gemini batch job found for upload")

    cancel_results: list[dict[str, Any]] = []
    if chunk_job_names:
        # Best-effort cancel each chunk
        for jn in chunk_job_names:
            try:
                resp = await asyncio.to_thread(_gemini_batch_cancel_sync, jn)
                cancel_results.append({"job_name": jn, "status": "cancel_requested", "response": resp})
            except Exception as exc:
                cancel_results.append({"job_name": jn, "status": "error", "error": str(exc)})
    else:
        # Legacy single-job cancel
        try:
            resp = await asyncio.to_thread(_gemini_batch_cancel_sync, top_level_job_name)
            cancel_results.append({"job_name": top_level_job_name, "status": "cancel_requested", "response": resp})
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Gemini batch cancel failed: {str(exc)}") from exc

    async with get_upload_lock(upload_id):
        try:
            state = await read_upload_artifact(upload_id, "state")
            current_batch = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
            state["gemini_batch"] = {
                **current_batch,
                "status": "cancel_requested",
            }
            await persist_upload_state(upload_id, state)
        except Exception:
            pass

    _invalidate_gemini_batch_list_cache()
    return {
        "upload_id": upload_id,
        "status": "cancel_requested",
        "cancelled_jobs": cancel_results,
    }


@app.post("/batch/jobs/status")
async def batch_job_get_status_by_name(job_name: str = Query(..., min_length=1)) -> dict[str, Any]:
    try:
        batch_obj = await asyncio.to_thread(_gemini_batch_get_sync, job_name)
        live_state = _gemini_batch_state_name(batch_obj)
        done_flag = bool(batch_obj.get("done"))
        error_obj = batch_obj.get("error")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini batch status fetch failed: {str(exc)}") from exc

    return {
        "job_name": job_name,
        "batch_status": _derive_ui_batch_status(
            live_state=live_state,
            done_flag=done_flag,
            error_obj=error_obj,
            local_status=None,
        ),
        "live_state": live_state,
        "done": done_flag,
        "raw": batch_obj,
    }


async def _sync_batch_job_action_local_state(
    upload_id: str,
    job_name: str,
    action: str,
    expected_generation: Optional[int] = None,
) -> bool:
    """Best-effort reconciliation after a destructive by-name remote action."""
    async with get_upload_lock(upload_id):
        try:
            state = await read_upload_artifact(upload_id, "state")
            batch = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
            current_generation = _batch_generation(state)
            if (
                expected_generation is not None
                and expected_generation != current_generation
            ):
                return False
            chunks = batch.get("chunks") if isinstance(batch.get("chunks"), list) else []
            top_matches = str(batch.get("job_name") or "").strip() == job_name
            matching_chunks = [
                chunk for chunk in chunks
                if isinstance(chunk, dict)
                and str(chunk.get("job_name") or "").strip() == job_name
            ]

            if action == "cancel":
                if not top_matches and not matching_chunks:
                    return False
                for chunk in matching_chunks:
                    chunk["status"] = "cancel_requested"
                batch["status"] = (
                    "cancel_requested" if top_matches
                    else _aggregate_batch_chunk_status(chunks)
                )
            elif action == "delete":
                # An unmatched operation can be a just-created chunk whose
                # metadata has not persisted yet. Require the generation from
                # the listing so an old operation cannot mark a newer retry.
                if (
                    not top_matches
                    and not matching_chunks
                    and expected_generation is None
                ):
                    return False
                deleted_at = _now_iso()
                tombstone = await _write_batch_deletion_tombstone(
                    upload_id,
                    state,
                    job_name,
                    deleted_at,
                    top_level_deleted=top_matches,
                )
                _apply_batch_deletion_tombstone(state, tombstone)
            else:
                return False

            state["gemini_batch"] = batch
            await persist_upload_state(upload_id, state)
            return True
        except (FileNotFoundError, KeyError):
            return False
        except Exception as exc:
            print(f"[batch-manager] local {action} sync failed for {upload_id}: {exc!r}")
            return False


@app.post("/batch/jobs/cancel")
async def batch_job_cancel_by_name(
    job_name: str = Query(..., min_length=1),
    upload_id: Optional[str] = Query(None),
    expected_generation: Optional[int] = Query(None, ge=0),
) -> dict[str, Any]:
    try:
        cancel_resp = await asyncio.to_thread(_gemini_batch_cancel_sync, job_name)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini batch cancel failed: {str(exc)}") from exc
    local_state_updated = bool(upload_id) and await _sync_batch_job_action_local_state(
        str(upload_id), job_name, "cancel", expected_generation)
    _invalidate_gemini_batch_list_cache()
    return {
        "job_name": job_name,
        "status": "cancel_requested",
        "response": cancel_resp,
        "local_state_updated": local_state_updated,
    }


@app.post("/batch/jobs/delete")
async def batch_job_delete_by_name(
    job_name: str = Query(..., min_length=1),
    upload_id: Optional[str] = Query(None),
    expected_generation: Optional[int] = Query(None, ge=0),
) -> dict[str, Any]:
    try:
        delete_resp = await asyncio.to_thread(_gemini_batch_delete_sync, job_name)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini batch delete failed: {str(exc)}") from exc
    local_state_updated = bool(upload_id) and await _sync_batch_job_action_local_state(
        str(upload_id), job_name, "delete", expected_generation)
    _invalidate_gemini_batch_list_cache()
    return {
        "job_name": job_name,
        "status": "deleted",
        "response": delete_resp,
        "local_state_updated": local_state_updated,
    }


_RELATIONSHIP_FILES = ("confirmed_relation.csv", "notconfirmed_relation.csv",
                       "retry.csv", "report.json", "run.log")

# Keyed by BOTH status.json phases and report.json summary statuses (the two vocabularies
# do not collide). "stopped" maps to "completed" deliberately: run_detail.js's terminal
# set has no rule for it, so passing it through would poll forever and never render the
# Files card — and a stopped run IS terminal, with its outputs written.
_RELATIONSHIP_RUN_STATUS = {
    "queued": "queued", "scraping": "processing", "cleaning": "processing",
    "reporting": "processing", "completed": "completed",
    "completed_with_errors": "completed_with_errors",
    "stopped": "completed", "failed": "failed",
}
# Mirrors run_detail.js's ROW_TERMINAL_STATUSES.
_RELATIONSHIP_TERMINAL_STATUSES = {"completed", "completed_with_errors", "failed"}


_GMAPS_FILES = ("found.csv", "notFound.csv", "retry.csv", "report.json", "run.log")

# enriched/notEnriched, not found/notFound: this pipeline is HANDED the website, so the
# split that means something is "did we enrich it", not "did we discover it".
_FIRMOGRAPHICS_FILES = ("enriched.csv", "notEnriched.csv", "retry.csv",
                        "report.json", "run.log")


def _s3_run_available_files(prefix: str, names: tuple[str, ...]) -> list[str]:
    """Which of the run's output files actually exist.

    One scoped LIST per name (each returns 0 or 1 keys) rather than a single LIST of the
    run prefix: at 500k rows that prefix holds a million per-row objects.
    """
    from app.services.serpwow import s3_run_store as run_store

    return [name for name in names
            if next(run_store.iter_keys(f"{prefix}/{name}"), None) is not None]


def _s3_run_elapsed(counters: dict[str, Any]) -> Optional[int]:
    """Wall clock from an S3-only run's created_at to now, for mid-run display.

    report.json only exists once the run is terminal, so without this the Processing-time
    tile stayed blank for the whole run.
    """
    import calendar
    import time as _time

    try:
        started = calendar.timegm(
            _time.strptime(str(counters.get("created_at")), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None
    return max(0, int(_time.time() - started))


async def _s3_run_status(run_id: str, *, segment: str, pipeline: str,
                         files: tuple[str, ...],
                         fallback: Any) -> Optional[dict[str, Any]]:
    """Build the /status response for an S3-only run from its status.json counters.

    Same JSON shape as the state-driven pipelines, so run_detail.js is unchanged. Returns
    None when this id is not a run of this pipeline. `fallback` builds the mid-run summary
    for the window before report.json exists — the one part that differs per pipeline.
    """
    from app.services.serpwow import s3_run_store as rel_store

    pointer = await asyncio.to_thread(rel_store.read_run_pointer, run_id, segment)
    if not pointer:
        return None
    prefix = str(pointer.get("prefix") or "")
    counters = await asyncio.to_thread(rel_store.read_status, prefix) or {}
    phase = str(counters.get("phase") or "queued")
    total = int(counters.get("rows_total") or 0)
    scraped = int(counters.get("rows_scraped") or 0)
    failed = int(counters.get("rows_failed") or 0)

    report = await asyncio.to_thread(
        rel_store.get_object, f"{prefix}/report.json") or {}
    summary = report.get("summary") or fallback(counters, total, scraped, failed)
    # Prefer the status write_outputs computed — it is what _notify_terminal sent to
    # Supabase, so taking it here is what stops the detail page and the Runs list
    # disagreeing (phase only ever reaches "completed", never "completed_with_errors").
    #
    # But ONLY when phase == "completed", because report.json OUTLIVES its run:
    # retry_failed_rows deletes the error markers, not the outputs, so a re-driven run
    # sits at phase "scraping" with the PREVIOUS run's terminal report.json still in
    # place. write_outputs rewrites report.json immediately before setting the phase to
    # "completed", so that phase — and only that one — proves the report is this drive's.
    # ("stopped" needs no special case: write_outputs makes summary["status"] "stopped"
    # exactly when it sets that phase, and both map to "completed" below.)
    reported = str(summary.get("status") or "") if phase == "completed" else ""
    status = _RELATIONSHIP_RUN_STATUS.get(reported or phase, "processing")

    # Only advertise files that exist. A run that failed mid-scrape is terminal — so the
    # UI renders the Files card — but wrote none of the four; gsearch guards exactly this
    # case with available_files, and without it every link is an enabled 404. Gated on the
    # derived status, so a re-driven run (non-terminal again) advertises nothing either.
    available: list[str] = []
    if status in _RELATIONSHIP_TERMINAL_STATUSES:
        available = await asyncio.to_thread(_s3_run_available_files, prefix, files)

    return {
        "upload_id": run_id,
        "pipeline": pipeline,
        # Mid-run the report doesn't exist yet, so serve timings from the counters. The UI
        # reads processing_seconds_total/avg at the top level, not inside the summary.
        "created_at": counters.get("created_at"),
        "processing_seconds_total": summary.get("processing_seconds_total"),
        "processing_seconds_avg": summary.get("processing_seconds_avg"),
        "company_name": pointer.get("company_name"),
        "status": status,
        "total_rows": total,
        "processed_rows": scraped + failed,
        "failed_rows": failed,
        "phase": phase,
        "updated_at": counters.get("updated_at"),
        "serpwow_summary": {**summary, "available_files": available},
        "files": list(files),
    }


def _relationship_fallback_summary(counters: dict[str, Any], total: int, scraped: int,
                                   failed: int) -> dict[str, Any]:
    """The summary shown while a relationship run is still in flight."""
    return {
        "total_rows": total, "websites_found": 0,
        "websites_not_found": max(0, total - scraped),
        "confidence_mode": "llm",
        # Always true for this pipeline — phase 2 is always the Gemini Batch verdict pass,
        # there is no per-row LLM path. Must match relationship_outputs' summary so the
        # Batch chip doesn't flip from "On" to "Off" while a run is still in progress.
        "is_batch": True,
        # Mid-run the LLM hasn't reported usage yet (it arrives with the batch results), so
        # these are honest zeros rather than absent keys — the tiles render 0, not blank,
        # and fill in for real once report.json exists.
        "model": llm_batch.batch_model(),
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "processing_seconds_total": _s3_run_elapsed(counters),
        "phase_seconds": {"scraping": int(counters.get("scrape_seconds") or 0),
                          "cleaning": int(counters.get("llm_seconds") or 0)},
        "outcome_breakdown": {"found": 0, "not_found": 0, "errored": failed},
        "empty_response_breakdown": {
            "empty": int(counters.get("rows_billed_empty") or 0)},
        "cost": {"scrapedo_requests": int(counters.get("requests") or 0),
                 "scrapedo_credits": int(counters.get("credits") or 0),
                 "scrapedo_error_requests": 0, "scrapedo_billed_empty":
                     int(counters.get("rows_billed_empty") or 0),
                 "llm_usd": 0.0, "total_usd": 0.0},
    }


def _gmaps_fallback_summary(counters: dict[str, Any], total: int, scraped: int,
                            failed: int) -> dict[str, Any]:
    """The summary shown while a gmaps run is still in flight.

    No model, no tokens, no batch: heuristic confidence is computed inside the row task,
    so there is nothing deferred to report on.
    """
    from app.services.serpwow.scrapedo_maps_client import CREDITS_PER_CALL

    # Billed 200s aren't a counter of their own — credits ARE 10 per billed 200, so the
    # split is exact arithmetic, not an estimate. Mid-run the billing card needs it to
    # say "87 of 100 rows billed"; report.json carries the real per-row sum at the end.
    requests = int(counters.get("requests") or 0)
    billed = int(counters.get("credits") or 0) // CREDITS_PER_CALL
    return {
        "total_rows": total, "websites_found": 0,
        "websites_not_found": max(0, total - scraped),
        "confidence_mode": "heuristic",
        "is_batch": False,
        "model": None,
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "processing_seconds_total": _s3_run_elapsed(counters),
        "phase_seconds": {"scraping": int(counters.get("scrape_seconds") or 0)},
        "outcome_breakdown": {"found": 0, "not_found": 0, "errored": failed},
        "empty_response_breakdown": {
            "no_listing": int(counters.get("rows_no_listing") or 0),
            "billed_empty": int(counters.get("rows_billed_empty") or 0)},
        "cost": {"scrapedo_requests": requests,
                 "scrapedo_successful_requests": billed,
                 "scrapedo_failed_requests": max(0, requests - billed),
                 "scrapedo_credits": int(counters.get("credits") or 0),
                 "scrapedo_error_requests": 0,
                 "scrapedo_billed_empty": int(counters.get("rows_billed_empty") or 0),
                 "llm_usd": 0.0, "total_usd": 0.0},
    }


def _firmographics_fallback_summary(counters: dict[str, Any], total: int, scraped: int,
                                    failed: int) -> dict[str, Any]:
    """The summary shown while a firmographics run is still in flight.

    Credits are two rates here, so billed-call counts cannot be derived from the credit
    total the way gmaps derives them (10 per call). Mid-run the split is unknown, so the
    per-endpoint keys stay 0 and report.json fills them in with the real per-row sums —
    honest zeros rather than a guess that would contradict the final card.
    """
    requests = int(counters.get("requests") or 0)
    credits = int(counters.get("credits") or 0)
    return {
        "total_rows": total,
        "websites_found": int(counters.get("rows_cleaned") or 0) or scraped,
        "websites_not_found": max(0, total - scraped),
        # No confidence concept — the website is an input. None makes the UI skip the chip.
        "confidence_mode": None,
        "is_batch": bool(counters.get("rows_cleaned")),
        "llm_mode": None,
        "model": llm_batch.batch_model(),
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "processing_seconds_total": _s3_run_elapsed(counters),
        "phase_seconds": {"scraping": int(counters.get("scrape_seconds") or 0),
                          "cleaning": int(counters.get("llm_seconds") or 0)},
        "outcome_breakdown": {"found": 0, "not_found": 0, "errored": failed},
        "empty_response_breakdown": {"no_ai_overview": 0, "deferred": 0,
                                     "no_website": 0, "never_processed": 0},
        "cost": reporting._build_cost(0.0, 0, 0, requests, credits, 0, 0),
    }


async def _firmographics_status(run_id: str) -> Optional[dict[str, Any]]:
    from app.services.serpwow.firmographics_runner import (
        PIPELINE_SEGMENT as FIRMO_SEGMENT,
    )

    return await _s3_run_status(
        run_id, segment=FIRMO_SEGMENT, pipeline=PIPELINE_FIRMOGRAPHICS,
        files=_FIRMOGRAPHICS_FILES, fallback=_firmographics_fallback_summary)


async def _relationship_status(run_id: str) -> Optional[dict[str, Any]]:
    """Build the /status response for a relationship run. None if it is not one."""
    from app.services.serpwow import s3_run_store as rel_store

    return await _s3_run_status(
        run_id, segment=rel_store.PIPELINE_SEGMENT, pipeline=PIPELINE_RELATIONSHIP,
        files=_RELATIONSHIP_FILES, fallback=_relationship_fallback_summary)


async def _gmaps_status(run_id: str) -> Optional[dict[str, Any]]:
    """Build the /status response for a gmaps run. None if it is not one."""
    from app.services.serpwow.gmaps_runner import PIPELINE_SEGMENT as GMAPS_SEGMENT

    return await _s3_run_status(
        run_id, segment=GMAPS_SEGMENT, pipeline=PIPELINE_GMAPS,
        files=_GMAPS_FILES, fallback=_gmaps_fallback_summary)


@app.get("/uploads/{upload_id}/status")
async def upload_status(upload_id: str) -> dict[str, Any]:
    # The S3-only pipelines have no state.json — they are counter-driven. Check their
    # pointers first; the response shape is identical so the UI needs no change.
    for build in (_relationship_status, _gmaps_status, _firmographics_status):
        s3_status = await build(upload_id)
        if s3_status is not None:
            return s3_status

    try:
        state = await get_upload_state(upload_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload ID not found") from exc

    await maybe_resume_gemini_batch_for_upload(upload_id, state)
    try:
        state = await get_upload_state(upload_id)
    except KeyError:
        pass
    state = await maybe_reconcile_gemini_batch_status(upload_id, state)

    state = await maybe_fail_stale_processing_rows(upload_id, state)
    summary = summarize_upload_state(dict(state))
    # SerpWow pipelines (gsearch/gmaps) surface a confidence/cost summary (model,
    # batch mode, found counts, cost, tokens) so the run-detail UI can show the
    # same tiles AI Mode does. gmaps has no LLM -> model=None, tokens=0.
    serpwow_summary = None
    if (summary.get("pipeline") or "") in COST_SUMMARY_PIPELINES:
        try:
            gs = reporting.build_summary(
                summary, reporting.state_to_entity_results(summary))
            upload_terminal = summary.get("status") in {
                "completed", "completed_with_errors", "failed",
            }
            batch = summary.get("gemini_batch")
            batch_settled = not isinstance(batch, dict) or batch.get("status") in {
                "succeeded", "completed_with_errors", "failed", "cancelled",
                "skipped", "not_started",
            }
            available_files = []
            if upload_terminal and batch_settled:
                available_files = await _available_reporting_files(
                    upload_id,
                    str(summary.get("company_name") or ""),
                    str(summary.get("pipeline") or ""),
                )
            serpwow_summary = {
                "websites_found": gs["websites_found"],
                "websites_not_found": gs["websites_not_found"],
                "outcome_breakdown": gs["outcome_breakdown"],
                "error_breakdown": gs["error_breakdown"],
                "available_files": available_files,
                "model": gs["model"],
                "confidence_mode": gs["confidence_mode"],
                "is_batch": gs["is_batch"],
                "cost": gs["cost"],
                "token_usage": gs["token_usage"],
                # blank_rows / searchable_rows / unique_pairs / total_rows_original were
                # dropped with relationship's (X, Y) dedup — nothing produces them and
                # nothing in the UI reads them.
                **{k: gs[k] for k in ("relationship_breakdown",
                                      "empty_response_breakdown") if k in gs},
            }
        except Exception:
            serpwow_summary = None
    return {
        "upload_id": summary["upload_id"],
        "pipeline": summary.get("pipeline") or "",
        "status": summary["status"],
        "gemini_batch": summary.get("gemini_batch"),
        "serpwow_summary": serpwow_summary,
        "queue_recovery": summary.get("queue_recovery"),
        "created_at": summary.get("created_at"),
        "updated_at": summary.get("updated_at"),
        "total_rows": summary["total_rows"],
        "processed_rows": summary["processed_rows"],
        "success_rows": summary["success_rows"],
        "failed_rows": summary["failed_rows"],
        "processing_seconds_total": summary.get("processing_seconds_total", 0.0),
        "processing_seconds_avg": summary.get("processing_seconds_avg", 0.0),
        "processing_seconds_count": summary.get("processing_seconds_count", 0),
        "file_links": _upload_file_links(upload_id, str(summary.get("company_name") or ""), str(summary.get("pipeline") or "")),
        "rows": [
            {
                "row_index": row["row_index"],
                "company_name": row["company_name"],
                "country": row["country"],
                "firm_id": row.get("firm_id"),
                "industry": row.get("industry"),
                "full_address": row.get("full_address"),
                "official_website": row.get("official_website"),
                "status": row["status"],
                "error": row.get("error"),
                "raw_response_s3_key": _raw_response_key(row),
            }
            for row in summary.get("rows", [])
        ],
    }


async def _relationship_failure_analysis(
    run_id: str, sample_limit: int,
) -> Optional[dict[str, Any]]:
    """build_failure_analysis's shape, sourced from errors/ objects instead of rows[].

    The "View failed rows" control run_detail.js offers whenever outcome_breakdown.errored
    is non-zero calls this; without a branch here a relationship run answered 404 and the
    page showed a red error under its own button.

    ``failed_rows`` is exact (one LIST of errors/, the same scan list_done_rows does). The
    aggregate buckets are computed over the SAMPLE only — the alternative is GETting up to
    500k error objects to answer a debug panel that reads neither.
    """
    from app.services.serpwow import s3_run_store as rel_store

    pointer, _segment = await asyncio.to_thread(_find_s3_run, run_id)
    if not pointer:
        return None
    prefix = str(pointer.get("prefix") or "")
    limit = max(1, int(sample_limit))

    def _by_row_index(key: str) -> tuple[int, int, str]:
        # NOT plain sorted(): shard directories are numeric but unpadded, so errors/10/
        # sorts before errors/2/ and the "first N" sample becomes a lexicographic slice
        # once a run exceeds ten shards.
        idx = rel_store._idx_from_key(key)
        return (1, 0, key) if idx is None else (0, idx, "")

    def _collect() -> tuple[int, list[dict[str, Any]]]:
        keys = list(rel_store.iter_keys(f"{prefix}/errors/"))
        rows: list[dict[str, Any]] = []
        for key in sorted(keys, key=_by_row_index)[:limit]:
            envelope = rel_store.get_object(key) or {}
            fields = envelope.get("fields") or {}
            rows.append({
                "row_index": envelope.get("row_index"),
                "company_name": fields.get("y_name"),
                # How many scrape.do calls this row actually cost before it died. Without
                # it a row that burned all SCRAPEDO_MAX_RETRIES reads exactly like a row
                # that was tried once, and the retries look like they were never there.
                "attempts": envelope.get("request_count"),
                # Only scrape.do can produce an error object — the verdict and reporting
                # phases never write one (see relationship_runner._scrape_one).
                "error_source": "scrapedo",
                "error_category": envelope.get("error_category"),
                "error": envelope.get("error"),
                "official_website": None,
                "status_updated_at": None,
            })
        rows.sort(key=lambda row: (row["row_index"] is None, row["row_index"]))
        return len(keys), rows

    counters = await asyncio.to_thread(rel_store.read_status, prefix) or {}
    failed_count, sample = await asyncio.to_thread(_collect)
    total_rows = int(counters.get("rows_total") or 0)

    def _buckets(field: str, name: str) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in sample:
            value = str(row.get(field) or "").strip()
            if value:
                counts[value] = counts.get(value, 0) + 1
        return [{name: k, "count": v} for k, v in
                sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]]

    return {
        "upload_id": run_id,
        "status": str(counters.get("phase") or ""),
        "gemini_batch": None,
        "total_rows": total_rows,
        "failed_rows": failed_count,
        "failed_rate_pct": (round((failed_count / total_rows) * 100, 2)
                            if total_rows else 0.0),
        "failed_missing_official_website": failed_count,
        "error_buckets": _buckets("error", "reason"),
        "search_attempt_error_buckets": [],
        "by_source": _buckets("error_source", "source"),
        "by_category": _buckets("error_category", "category"),
        "sample_failed_rows": sample,
    }


@app.get("/uploads/{upload_id}/failure-analysis")
async def upload_failure_analysis(
    upload_id: str,
    sample_limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    relationship_analysis = await _relationship_failure_analysis(upload_id, sample_limit)
    if relationship_analysis is not None:
        return relationship_analysis

    try:
        state = await get_upload_state(upload_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload ID not found") from exc

    await maybe_resume_gemini_batch_for_upload(upload_id, state)
    try:
        state = await get_upload_state(upload_id)
    except KeyError:
        pass
    state = await maybe_reconcile_gemini_batch_status(upload_id, state)
    state = await maybe_fail_stale_processing_rows(upload_id, state)
    summary = summarize_upload_state(dict(state))
    return build_failure_analysis(summary, sample_limit=sample_limit)


@app.get("/uploads/{upload_id}/output")
async def upload_output(
    upload_id: str,
    download: bool = Query(False),
    format: Literal["json", "csv", "xlsx"] = Query("json"),
) -> Response:
    try:
        output_data = await read_upload_artifact(upload_id, "output")
    except FileNotFoundError:
        try:
            state = await get_upload_state(upload_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Output not found for upload ID") from exc
        output_data = build_upload_output_payload(summarize_upload_state(dict(state)))
    except Exception as exc:
        if "NoSuchKey" in str(exc):
            try:
                state = await get_upload_state(upload_id)
            except KeyError as key_exc:
                raise HTTPException(status_code=404, detail="Output not found for upload ID") from key_exc
            output_data = build_upload_output_payload(summarize_upload_state(dict(state)))
        else:
            raise

    if isinstance(output_data, dict):
        output_data.update(build_processing_timing_summary(output_data.get("results") or []))

    if format == "csv":
        # charset=utf-8 must be declared: the bytes carry a BOM for Excel, but a browser
        # previewing them inline honours the header, not the BOM.
        body = build_upload_output_csv_bytes(output_data)
        headers = {}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{upload_id}.csv"'
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers=headers,
        )

    if format == "xlsx":
        body = build_upload_output_xlsx_bytes(output_data)
        headers = {}
        if download:
            headers["Content-Disposition"] = (
                f'attachment; filename="{upload_id}.xlsx"'
            )
        return Response(
            content=body,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers=headers,
        )

    body = json.dumps(output_data, ensure_ascii=True, indent=2)
    if download:
        headers = {"Content-Disposition": f'attachment; filename="{upload_id}.json"'}
        return Response(content=body, media_type="application/json", headers=headers)
    return Response(content=body, media_type="application/json")


@app.get("/uploads/{upload_id}/result")
async def upload_result_file(
    upload_id: str,
    file: str = Query(...),
    download: bool = Query(False),
) -> Response:
    from app.services.serpwow import s3_run_store as rel_store

    pointer, segment = await asyncio.to_thread(_find_s3_run, upload_id)
    if pointer:
        per_segment = {
            "gmaps": {"found.csv", "notFound.csv"},
            "firmographics": {"enriched.csv", "notEnriched.csv"},
        }
        allowed = per_segment.get(
            segment, {"confirmed_relation.csv", "notconfirmed_relation.csv"}) | {
            "retry.csv", "report.json", "run.log", "input.csv", "status.json"}
        if file not in allowed:
            raise HTTPException(status_code=400, detail=f"Unknown file {file!r}")
        data = await asyncio.to_thread(
            rel_store.get_bytes, f"{pointer['prefix']}/{file}")
        if data is None:
            raise HTTPException(status_code=404, detail=f"{file} not available yet")
        media = "application/json" if file.endswith(".json") else "text/plain"
        return Response(content=data, media_type=media)

    if file not in _GSEARCH_RESULT_FILES:
        raise HTTPException(status_code=400, detail="file not allowed")
    upload_dir = _find_upload_dir(upload_id)
    path = upload_dir / file
    data: Optional[bytes] = None
    if path.exists():
        data = path.read_bytes()
    elif os.getenv("S3_BUCKET"):
        key = await asyncio.to_thread(_find_s3_upload_key_sync, upload_id, file)
        if key is not None:
            from app.core import s3 as core_s3
            def _get() -> bytes:
                return core_s3.get_s3_client().get_object(
                    Bucket=core_s3.bucket_name(), Key=key)["Body"].read()
            try:
                data = await asyncio.to_thread(_get)
            except Exception:
                data = None
    if data is None:
        raise HTTPException(status_code=404, detail="file not found")
    if file.endswith(".csv"):
        media = "text/csv"
    elif file.endswith(".json"):
        media = "application/json"
    else:
        media = "text/plain"
    headers = {"Content-Disposition": f'attachment; filename="{file}"'} if download else {}
    return Response(content=data, media_type=media, headers=headers)


# The old /gmaps/discover + /gmaps/details debug routes are gone with the SerpWow
# two-step flow: there is no data_cid discovery step to inspect any more, and
# scrape.do's maps/search returns the website inline (see scrapedo_maps_client).
@app.get("/gmaps/search")
async def gmaps_search(q: str, country: Optional[str] = None) -> dict[str, Any]:
    started_monotonic = asyncio.get_running_loop().time()
    if not os.getenv("SCRAPEDO_TOKEN", "").strip():
        raise HTTPException(status_code=400, detail="SCRAPEDO_TOKEN is not configured")

    try:
        from app.services.serpwow import scrapedo_maps_client as gmaps_module
        res = await gmaps_module.process_gmaps_query(q, gl=_country_to_gl(country))
        response = dict(res) if isinstance(res, dict) else {"result": res}
        response["processing_seconds"] = round(asyncio.get_running_loop().time() - started_monotonic, 3)
        return response
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))




@app.get("/gsearch/discover")
async def gsearch_discover(
    company_name: str,
    country: str,
    parsed_city_state: Optional[str] = "",
    full_address: Optional[str] = "",
    industry: Optional[str] = "",
    phase: str = Query("all"),
    people: Optional[list[str]] = Query(None),
    trade_names: Optional[list[str]] = Query(None),
) -> dict[str, Any]:
    started_monotonic = asyncio.get_running_loop().time()
    api_key = os.getenv("SERPWOW_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="SERPWOW_API_KEY is not configured")
        
    queries = build_selected_phase_queries(
        company_name=company_name,
        country=country,
        parsed_city_state=parsed_city_state,
        full_address=full_address,
        industry=industry,
        phase=phase,
    )
    
    if phase == "phase5":
        target_people = people or []
        target_trade_names = trade_names or []
        
        if not target_people and not target_trade_names:
            phase1_queries = build_selected_phase_queries(
                company_name=company_name,
                country=country,
                parsed_city_state=parsed_city_state,
                full_address=full_address,
                industry=industry,
                phase="phase1",
            )
            if phase1_queries:
                quick_res = await run_serpwow_search(phase1_queries[0][1], country=country)
                raw_resp = quick_res.get("raw_response")
                extracted_people, extracted_trade_names = _extract_phase5_pivots_from_serpwow(raw_resp)
                target_people.extend(extracted_people)
                target_trade_names.extend(extracted_trade_names)
        
        clean_company = _normalize_location_token(company_name)
        clean_country = _normalize_location_token(country)
        variants = _company_name_variants(clean_company)
        v_primary = variants[0] if variants else clean_company
        
        plot_raw = _extract_address_component(full_address, ("plot",)) if full_address else ""
        road_raw = _extract_address_component(full_address, ("road", " rd")) if full_address else ""
        plot_hyphen, _ = _marker_variants(plot_raw) if plot_raw else ("", "")
        road_hyphen, _ = _marker_variants(road_raw) if road_raw else ("", "")
        
        phase5_attempt_queries = []
        for person in target_people:
            q = f'"{person}" "{v_primary}"'
            phase5_attempt_queries.append(("phase5_person_connection", q))
            
        for trade in target_trade_names:
            if plot_hyphen and road_hyphen:
                q = f'"{trade}" "{plot_hyphen}" "{road_hyphen}"'
                phase5_attempt_queries.append(("phase5_trade_address_connection", q))
            else:
                q = f'"{trade}" "{v_primary}" {clean_country}'
                phase5_attempt_queries.append(("phase5_trade_connection", q))
            
            q_web = f'"{trade}" official website {clean_country}'
            phase5_attempt_queries.append(("phase5_trade_official_website", q_web))
            
        queries = phase5_attempt_queries
        
    timeout_sec = _get_float_env("SERPWOW_TIMEOUT_SEC", 45.0)
    async with httpx.AsyncClient(timeout=timeout_sec) as serpwow_client:
        results = await asyncio.gather(
            *[
                run_serpwow_search(q[1], country=country, client=serpwow_client)
                for q in queries
            ],
            return_exceptions=True,
        )
        
    formatted_results = []
    candidates = []
    seen_candidates = set()
    
    for (label, query), raw_result in zip(queries, results):
        if isinstance(raw_result, Exception):
            raw_result = {
                "provider": "serpwow",
                "used": False,
                "query": query,
                "official_website": None,
                "candidates": [],
                "status_code": None,
                "search_url": None,
                "raw_response": None,
                "error": f"{type(raw_result).__name__}: {str(raw_result)}",
                "error_category": categorize_http_error(
                    None, f"{type(raw_result).__name__}: {raw_result}"),
            }

        attempt_cands = raw_result.get("candidates") or []
        for cand in attempt_cands:
            if cand and cand not in seen_candidates and not is_disallowed_official_url(cand):
                seen_candidates.add(cand)
                candidates.append(cand)

        formatted_results.append({
            "phase": label,
            "query": query,
            "success": bool(raw_result.get("used")),
            "error": raw_result.get("error"),
            "error_category": raw_result.get("error_category"),
            "status_code": raw_result.get("status_code"),
            "search_url": raw_result.get("search_url"),
            "raw_response": raw_result.get("raw_response"),
        })
        
    return {
        "company_name": company_name,
        "country": country,
        "phase": phase,
        "queries_run": len(queries),
        "candidates": candidates,
        "results": formatted_results,
        "processing_seconds": round(asyncio.get_running_loop().time() - started_monotonic, 3),
    }
