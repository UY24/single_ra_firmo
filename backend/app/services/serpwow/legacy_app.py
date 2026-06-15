import asyncio
import csv
import io
import json
import os
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
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
                # Allow .env to fill keys that are missing OR present-but-empty.
                if key and (key not in os.environ or not os.environ.get(key)):
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
upload_active_rows: dict[str, set[int]] = {}
upload_active_rows_lock = asyncio.Lock()
upload_summaries_cache: dict[str, dict[str, Any]] = {}
gemini_batch_list_cache: list[dict[str, Any]] = []
gemini_batch_list_cache_fetched_at: float = 0.0
gemini_batch_list_error_cooldown_until: float = 0.0

s3_client = None


class CrawlRequest(BaseModel):
    company_name: str
    country: str
    firm_id: Optional[str] = None
    industry: Optional[str] = None
    full_address: Optional[str] = None


class FirmographicsRequest(BaseModel):
    official_website: str
    company_name: Optional[str] = None
    country: Optional[str] = None
    firm_id: Optional[str] = None
    industry: Optional[str] = None
    full_address: Optional[str] = None


class CrawlResponse(BaseModel):
    company_name: str
    country: str
    firm_id: Optional[str] = None
    input_industry: Optional[str] = None
    input_full_address: Optional[str] = None
    official_website: Optional[str]
    summary: str
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    industry: Optional[str] = None
    products: list[str] = []
    services: list[str] = []
    website_company_descirption_ai: Optional[str] = None
    website_company_descirption_translated_ai: Optional[str] = None
    massive_proxy_cost_usd: float
    serpwow_cost_usd: float
    gemini_cost_usd: float
    total_cost_usd: float
    context: dict[str, Any]


SERPWOW_API_URL = "https://api.serpwow.com/live/search"
PIPELINE_FULL = "full"
PIPELINE_URL_DISCOVERY = "url_discovery"
PIPELINE_FIRMOGRAPHICS = "firmographics"
PIPELINE_GMAPS = "gmaps"
PIPELINE_GSEARCH = "gsearch"


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        # Accept both "Z" and "+00:00" UTC representations.
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _get_float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _get_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _short_text(value: Any, limit: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:max(0, limit - 3)]}..."


def _pipeline_logs_enabled() -> bool:
    # Verbose-by-default for testing and triage.
    return _get_bool_env("ENABLE_VERBOSE_ROW_LOGS", True)


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


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
    "in": "in",
    "bangladesh": "bd",
    "bd": "bd",
    "canada": "ca",
    "ca": "ca",
    "australia": "au",
    "au": "au",
    "germany": "de",
    "de": "de",
    "france": "fr",
    "fr": "fr",
    "italy": "it",
    "it": "it",
    "spain": "es",
    "es": "es",
    "netherlands": "nl",
    "nl": "nl",
    "sweden": "se",
    "se": "se",
    "norway": "no",
    "no": "no",
    "denmark": "dk",
    "dk": "dk",
    "finland": "fi",
    "fi": "fi",
    "japan": "jp",
    "jp": "jp",
    "south korea": "kr",
    "korea": "kr",
    "kr": "kr",
    "china": "cn",
    "cn": "cn",
    "singapore": "sg",
    "sg": "sg",
    "united arab emirates": "ae",
    "uae": "ae",
    "ae": "ae",
    "saudi arabia": "sa",
    "sa": "sa",
    "qatar": "qa",
    "qa": "qa",
    "kuwait": "kw",
    "kw": "kw",
    "oman": "om",
    "om": "om",
    "bahrain": "bh",
    "bh": "bh",
    "ireland": "ie",
    "ie": "ie",
    "poland": "pl",
    "pl": "pl",
    "switzerland": "ch",
    "ch": "ch",
    "austria": "at",
    "at": "at",
    "belgium": "be",
    "be": "be",
    "portugal": "pt",
    "pt": "pt",
    "mexico": "mx",
    "mx": "mx",
    "brazil": "br",
    "br": "br",
    "argentina": "ar",
    "ar": "ar",
    "south africa": "za",
    "za": "za",
    "new zealand": "nz",
    "nz": "nz",
}


def _country_to_gl(country: Optional[str]) -> str:
    value = str(country or "").strip().lower()
    if not value:
        return "us"
    if value in COUNTRY_GL_ALIASES:
        return COUNTRY_GL_ALIASES[value]
    compact = re.sub(r"[^a-z]", "", value)
    if compact in COUNTRY_GL_ALIASES:
        return COUNTRY_GL_ALIASES[compact]
    if len(compact) == 2:
        return compact
    return "us"


def _upload_dir(upload_id: str, company_name: str = "") -> Path:
    safe = _safe_name(company_name) if company_name else ""
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
    if rabbitmq_queue is None:
        return None
    try:
        declare_result = await rabbitmq_queue.declare(passive=True)
        count = getattr(declare_result, "message_count", None)
        if count is None:
            return None
        return max(0, int(count))
    except Exception:
        return None


def _build_row_job_payload(upload_id: str, row: dict[str, Any], pipeline: str, phase: str = "all") -> dict[str, Any]:
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
    }


def _upload_s3_prefix(upload_id: str, company_name: str = "") -> str:
    safe = _safe_name(company_name) if company_name else ""
    return f"{S3_PREFIX}/{safe}/{upload_id}" if safe else f"{S3_PREFIX}/{upload_id}"


def _state_s3_key(upload_id: str, company_name: str = "") -> str:
    return f"{_upload_s3_prefix(upload_id, company_name)}/state.json"


def _output_s3_key(upload_id: str, company_name: str = "") -> str:
    return f"{_upload_s3_prefix(upload_id, company_name)}/output.json"


def _batch_input_jsonl_s3_key(upload_id: str, company_name: str = "") -> str:
    return f"{_upload_s3_prefix(upload_id, company_name)}/gemini_batch_input.jsonl"


def _batch_output_json_s3_key(upload_id: str, company_name: str = "") -> str:
    return f"{_upload_s3_prefix(upload_id, company_name)}/gemini_batch_output.json"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")


def _safe_name(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip())
    return text.strip("_") or "item"


def _extract_official_website_from_serpwow(data: dict[str, Any]) -> Optional[str]:
    knowledge_graph = data.get("knowledge_graph")
    if isinstance(knowledge_graph, dict):
        kg_url = (knowledge_graph.get("website") or "").strip()
        if kg_url and not is_disallowed_official_url(kg_url):
            return kg_url

    answer_box = data.get("answer_box")
    if isinstance(answer_box, dict):
        for key in ("link", "url"):
            ans_url = (answer_box.get(key) or "").strip()
            if ans_url and not is_disallowed_official_url(ans_url):
                return ans_url

    ai_overview = data.get("ai_overview")
    if isinstance(ai_overview, dict):
        for source in ai_overview.get("ai_overview_sources", []) or []:
            if not isinstance(source, dict):
                continue
            src_url = (source.get("source_url") or "").strip()
            if src_url and not is_disallowed_official_url(src_url):
                return src_url

    for result in data.get("organic_results", []) or []:
        if not isinstance(result, dict):
            continue
        if _is_listing_or_profile_result(result):
            continue
        link = (result.get("link") or result.get("url") or "").strip()
        if link and not is_disallowed_official_url(link):
            return link

    return None


def _serpwow_ai_overview_is_ambiguous(data: dict[str, Any]) -> bool:
    ai_overview = data.get("ai_overview")
    if not isinstance(ai_overview, dict):
        return False
    contents = ai_overview.get("ai_overview_contents")
    if not isinstance(contents, list):
        return False
    joined = " ".join(
        (item.get("text") or "").strip().lower()
        for item in contents
        if isinstance(item, dict)
    )
    markers = (
        "multiple entities",
        "multiple companies",
        "similar entities",
        "similar names",
        "recommended to verify",
        "verify the specific industry",
        "no single, verified url",
        "no dedicated",
        "no single verified",
        "no dedicated top-level domain",
    )
    return any(marker in joined for marker in markers)


def _is_listing_or_profile_result(result: dict[str, Any]) -> bool:
    link = (result.get("link") or result.get("url") or "").strip().lower()
    title = (result.get("title") or "").strip().lower()
    snippet = (result.get("snippet") or "").strip().lower()
    displayed_link = (result.get("displayed_link") or "").strip().lower()
    combined_text = f"{title} {snippet} {displayed_link}"
    listing_terms = (
        "overview",
        "company profile",
        "companies",
        "import export data",
        "shipment",
        "trade data",
        "supplier",
        "member",
        "directory",
        "market insights",
        "annual report",
        "voter list",
        "election board",
        "knowledge center",
    )
    if any(term in combined_text for term in listing_terms):
        return True

    if not link:
        return False
    parsed = urlparse(link)
    path = (parsed.path or "").lower()
    profile_path_markers = (
        "/companies/",
        "/company/",
        "/profile/",
        "/supplier/",
        "/member/",
        "/directory/",
        "/organization/",
    )
    return any(marker in path for marker in profile_path_markers)


def _extract_official_website_candidates_from_serpwow(data: dict[str, Any]) -> list[str]:
    candidates: list[str] = []

    knowledge_graph = data.get("knowledge_graph")
    if isinstance(knowledge_graph, dict):
        kg_url = (knowledge_graph.get("website") or "").strip()
        if kg_url and not is_disallowed_official_url(kg_url):
            candidates.append(kg_url)

    answer_box = data.get("answer_box")
    if isinstance(answer_box, dict):
        for key in ("link", "url"):
            ans_url = (answer_box.get(key) or "").strip()
            if ans_url and not is_disallowed_official_url(ans_url):
                candidates.append(ans_url)

    ai_overview = data.get("ai_overview")
    if isinstance(ai_overview, dict):
        for source in ai_overview.get("ai_overview_sources", []) or []:
            if not isinstance(source, dict):
                continue
            src_url = (source.get("source_url") or "").strip()
            if src_url and not is_disallowed_official_url(src_url):
                candidates.append(src_url)

    for result in data.get("organic_results", []) or []:
        if not isinstance(result, dict):
            continue
        if _is_listing_or_profile_result(result):
            continue
        link = (result.get("link") or result.get("url") or "").strip()
        if link and not is_disallowed_official_url(link):
            candidates.append(link)

    seen: set[str] = set()
    unique_candidates: list[str] = []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            unique_candidates.append(url)
    return unique_candidates


async def run_serpwow_search(
    query: str,
    country: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    api_key = os.getenv("SERPWOW_API_KEY", "").strip()
    if not api_key:
        return {
            "provider": "serpwow",
            "used": False,
            "query": query,
            "official_website": None,
            "candidates": [],
            "status_code": None,
            "search_url": None,
            "raw_response": None,
            "error": "SERPWOW_API_KEY is not configured",
        }

    params = {
        "api_key": api_key,
        "q": query,
        "hl": "en",
        "engine": "google",
        "include_ai_overview": "true",
        "gl": _country_to_gl(country),
    }
    timeout_sec = _get_float_env("SERPWOW_TIMEOUT_SEC", 45.0)

    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout_sec) as owned_client:
                return await run_serpwow_search(query, country=country, client=owned_client)
        if search_fetch_semaphore is not None:
            async with search_fetch_semaphore:
                response = await client.get(SERPWOW_API_URL, params=params)
        else:
            response = await client.get(SERPWOW_API_URL, params=params)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return {
            "provider": "serpwow",
            "used": False,
            "query": query,
            "official_website": None,
            "candidates": [],
            "status_code": None,
            "search_url": None,
            "raw_response": None,
            "error": str(exc),
        }

    request_info = data.get("request_info", {}) if isinstance(data, dict) else {}
    serpwow_raw = data if isinstance(data, dict) else {}
    official_website = _extract_official_website_from_serpwow(serpwow_raw)
    ambiguity_detected = _serpwow_ai_overview_is_ambiguous(serpwow_raw)
    if ambiguity_detected:
        official_website = None
    candidates = _extract_official_website_candidates_from_serpwow(serpwow_raw)
    return {
        "provider": "serpwow",
        "used": True,
        "query": query,
        "official_website": official_website,
        "candidates": candidates,
        "status_code": response.status_code,
        "search_url": request_info.get("search_url") if isinstance(request_info, dict) else None,
        "raw_response": data,
        "error": "Ambiguous entity in AI overview; continuing search." if ambiguity_detected else None,
    }


def build_primary_search_query(company_name: str, country: str) -> str:
    return f'What is the official website of "{company_name}" in {country}?'


def build_industry_fallback_query(company_name: str, industry: str) -> str:
    return f'What is the official website of "{company_name}" in the {industry} industry?'


def build_address_fallback_query(company_name: str, full_address: str) -> str:
    return f'What is the official website of "{company_name}" at {full_address}?'


def _normalized_domain(url_or_domain: str) -> str:
    value = (url_or_domain or "").strip().lower()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    host = (parsed.netloc or parsed.path or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split("/")[0]


def _normalize_url_for_compare(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.netloc or parsed.path or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").rstrip("/")
    return f"{scheme}://{host}{path}"


def _candidate_domain_is_plausible_for_company(domain: str, company_name: str, country: str) -> bool:
    host = _normalized_domain(domain)
    if not host:
        return False

    company_tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", (company_name or "").lower())
        if len(token) >= 4 and token not in {"company", "corporation", "limited", "ltd", "group", "trading"}
    ]
    if any(token in host for token in company_tokens):
        return True

    # Country-code TLD alone is weak for short/generic company names.
    # Only treat it as plausible when we also have meaningful company tokens.
    country_gl = _country_to_gl(country)
    if company_tokens and country_gl and host.endswith(f".{country_gl}"):
        return True

    return False


def _official_website_looks_plausible(url: str, company_name: str, country: str) -> bool:
    host = _normalized_domain(url)
    if not host:
        return False
    return _candidate_domain_is_plausible_for_company(host, company_name, country)


def _extract_address_search_fragments(full_address: str, max_fragments: int = 3) -> list[str]:
    if not full_address:
        return []

    keyword_markers = (
        "plot",
        "road",
        "rd",
        "block",
        "house",
        "street",
        "st",
        "sector",
        "building",
        "tower",
        "avenue",
        "ave",
    )
    raw_parts = [part.strip() for part in re.split(r",|\||;|\n", full_address) if part.strip()]
    selected: list[str] = []
    seen: set[str] = set()

    for part in raw_parts:
        normalized = _normalize_location_token(part)
        if len(normalized) < 3:
            continue
        lowered = normalized.lower()
        has_digit = bool(re.search(r"\d", lowered))
        has_keyword = any(marker in lowered for marker in keyword_markers)
        if not (has_digit or has_keyword):
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        selected.append(normalized)
        if len(selected) >= max_fragments:
            break

    return selected


def _append_unique_attempt_query(
    attempt_queries: list[tuple[str, str]],
    seen_queries: set[str],
    label: str,
    query: str,
) -> None:
    normalized = re.sub(r"\s+", " ", (query or "").strip())
    if not normalized:
        return
    key = normalized.lower()
    if key in seen_queries:
        return
    seen_queries.add(key)
    attempt_queries.append((label, normalized))


def _company_name_variants(company_name: str) -> list[str]:
    base = _normalize_location_token(company_name)
    if not base:
        return []
    variants: list[str] = [base]
    initials_match = re.match(r"^\s*([A-Za-z](?:\s+[A-Za-z]){1,4})\s+(.+)$", base)
    if initials_match:
        letters = re.findall(r"[A-Za-z]", initials_match.group(1))
        tail = _normalize_location_token(initials_match.group(2))
        if letters and tail:
            dotted = ".".join(letters) + "."
            compact = "".join(letters)
            variants.append(f"{dotted} {tail}")
            variants.append(f"{compact} {tail}")
    deduped: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        key = variant.lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(variant.strip())
    return deduped


def _extract_address_component(full_address: str, keywords: tuple[str, ...]) -> str:
    parts = [_normalize_location_token(part) for part in re.split(r",|\|", full_address or "") if part.strip()]
    for part in parts:
        lowered = part.lower()
        if any(keyword in lowered for keyword in keywords):
            return part
    return ""


def _marker_variants(value: str) -> tuple[str, str]:
    marker = _normalize_location_token(value)
    if not marker:
        return "", ""
    spaced = re.sub(r"[-#:/]+", " ", marker)
    spaced = re.sub(r"\s+", " ", spaced).strip()
    words = spaced.split()
    if words:
        words[0] = words[0].title()
    spaced = " ".join(words).strip()
    hyphen = ""
    if words:
        if len(words) == 1:
            hyphen = words[0]
        else:
            suffix_tokens = words[1:]
            if any(token.startswith("(") and token.endswith(")") for token in suffix_tokens):
                hyphen = f"{words[0]}-{'-'.join([token for token in suffix_tokens if not (token.startswith('(') and token.endswith(')'))])}".strip("-")
                paren_tokens = [token for token in suffix_tokens if token.startswith("(") and token.endswith(")")]
                if paren_tokens:
                    hyphen = f"{hyphen} {' '.join(paren_tokens)}".strip()
            else:
                hyphen = f"{words[0]}-{'-'.join(words[1:])}"
    return hyphen.strip(), spaced.strip()


def _extract_locality_city_postal(
    full_address: str,
    parsed_city_state: str,
    country: str,
) -> tuple[str, str, str]:
    parts = _dedupe_location_parts(
        [_normalize_location_token(part) for part in re.split(r",|\|", full_address or "") if part.strip()]
    )
    country_key = _normalize_location_token(country).lower()
    city = ""
    locality = ""
    postal = ""
    parsed_parts = [part for part in _dedupe_location_parts(parsed_city_state.split()) if part]
    if parsed_parts:
        city = parsed_parts[0]

    street_keywords = ("plot", "road", "rd", "block", "house", "street", "sector", "building", "tower", "avenue")
    for part in parts:
        lowered = part.lower()
        if not postal:
            postal_match = re.search(r"\b\d{4,6}\b", part)
            if postal_match:
                postal = postal_match.group(0)
        if not city and part and not any(k in lowered for k in street_keywords):
            if lowered != country_key and not re.fullmatch(r"\d{3,6}", lowered):
                city = part
        if not locality and part and not any(k in lowered for k in street_keywords):
            if lowered not in {country_key, city.lower() if city else ""} and not re.fullmatch(r"\d{3,6}", lowered):
                locality = part

    if not city:
        city = locality
    if not locality:
        locality = city
    return locality, city, postal


def _looks_like_person_name(value: str) -> bool:
    text = _normalize_location_token(value)
    if not text:
        return False
    lowered = text.lower()
    corporate_markers = (" ltd", " limited", " llc", " inc", " corporation", " corp", " company", " group")
    if any(marker in f" {lowered}" for marker in corporate_markers):
        return False
    tokens = re.findall(r"[A-Za-z][A-Za-z'.-]*", text)
    if len(tokens) < 2 or len(tokens) > 6:
        return False
    capitalized = sum(1 for token in tokens if token and token[0].isupper())
    return capitalized >= 2


def _extract_phase5_pivots_from_serpwow(raw_response: Optional[dict[str, Any]]) -> tuple[list[str], list[str]]:
    if not isinstance(raw_response, dict):
        return [], []

    titles: list[str] = []
    ai_overview = raw_response.get("ai_overview")
    if isinstance(ai_overview, dict):
        for source in ai_overview.get("ai_overview_sources", []) or []:
            if isinstance(source, dict):
                title = _normalize_location_token(str(source.get("source_title") or ""))
                if title:
                    titles.append(title)
    for item in raw_response.get("organic_results", []) or []:
        if isinstance(item, dict):
            title = _normalize_location_token(str(item.get("title") or ""))
            if title:
                titles.append(title)

    people: list[str] = []
    trade_names: list[str] = []
    seen_people: set[str] = set()
    seen_trade: set[str] = set()
    corporate_terms = (
        "ltd",
        "limited",
        "llc",
        "inc",
        "corp",
        "corporation",
        "group",
        "motors",
        "museum",
        "auto",
        "trading",
    )

    for title in titles:
        head = title.split(" - ", 1)[0].split("|", 1)[0].strip()
        head = _normalize_location_token(head)
        if not head:
            continue
        lowered = head.lower()
        if _looks_like_person_name(head):
            person_key = lowered
            if person_key not in seen_people:
                seen_people.add(person_key)
                people.append(head)
        elif any(term in lowered for term in corporate_terms):
            if "overview" in lowered or "profile" in lowered or "directory" in lowered:
                continue
            trade_key = lowered
            if trade_key not in seen_trade:
                seen_trade.add(trade_key)
                trade_names.append(head)

    return people[:3], trade_names[:3]


def build_investigative_search_queries(
    company_name: str,
    country: str,
    parsed_city_state: str = "",
    full_address: str = "",
    industry: str = "",
    max_queries: int = 10,
) -> list[tuple[str, str]]:
    attempt_queries: list[tuple[str, str]] = []
    seen_queries: set[str] = set()

    clean_company = _normalize_location_token(company_name)
    clean_country = _normalize_location_token(country)
    clean_city_state = _normalize_location_token(parsed_city_state)
    clean_industry = _normalize_location_token(industry)

    variants = _company_name_variants(clean_company)
    v_primary = variants[0] if variants else clean_company
    v_punct = variants[1] if len(variants) > 1 else v_primary
    v_compact = variants[2] if len(variants) > 2 else v_primary

    plot_raw = _extract_address_component(full_address, ("plot",))
    road_raw = _extract_address_component(full_address, ("road", " rd"))
    block_raw = _extract_address_component(full_address, ("block",))
    house_raw = _extract_address_component(full_address, ("house",))
    plot_hyphen, plot_space = _marker_variants(plot_raw)
    road_hyphen, road_space = _marker_variants(road_raw)
    block_hyphen, _ = _marker_variants(block_raw)
    house_hyphen, _ = _marker_variants(house_raw)
    if not house_hyphen and plot_hyphen:
        number_match = re.search(r"\b(\d{1,5})\b", plot_hyphen)
        if number_match:
            house_hyphen = f"House-{number_match.group(1)}"

    locality, city, postal = _extract_locality_city_postal(full_address, clean_city_state, clean_country)
    location_phrase = " ".join(part for part in [locality, city] if part).strip() or clean_city_state or clean_country

    # Phase 1: Initial Hook (exact-ish dorks with quoted markers).
    if plot_hyphen and road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase1_exact_hook",
            f'"{v_primary}" "{plot_hyphen}" "{road_hyphen}" {location_phrase}'.strip(),
        )
    if plot_space and road_space:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase1_punctuation_variation",
            f'"{v_punct}" "{plot_space}" "{road_space}" {location_phrase}'.strip(),
        )
    if block_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase1_block_variation",
            f'"{v_compact}" "{block_hyphen}" {location_phrase}'.strip(),
        )
    if city and postal:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase1_city_postal",
            f'"{v_primary}" "{city} {postal}"',
        )
    elif location_phrase:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase1_city_country",
            f'"{v_primary}" "{location_phrase}"',
        )

    # Phase 2: AI Overview triggers (natural language synthesis prompts).
    if plot_space and road_space:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase2_business_name_at_address",
            (
                f'What business or trade name operates under "{v_primary}" at '
                f"{plot_space}, {road_space}, {location_phrase}?"
            ),
        )
    _append_unique_attempt_query(
        attempt_queries,
        seen_queries,
        "phase2_exec_or_owner",
        f'Who is the owner, CEO, or Managing Director of "{v_primary}" located in {location_phrase}?',
    )
    if block_hyphen and plot_hyphen and road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase2_registered_companies",
            (
                "What companies are registered at the address "
                f'{block_hyphen}, {plot_hyphen}, {road_hyphen}, {location_phrase}?'
            ),
        )
    if road_space:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase2_consumer_website",
            (
                f"Is there a consumer-facing website for {v_primary} "
                f"registered at {road_space} {location_phrase}?"
            ),
        )

    # Phase 3: Pivot (address-only discovery).
    if plot_hyphen and road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase3_address_pivot",
            f'"{plot_hyphen}" "{road_hyphen}" {location_phrase} -residential',
        )
    if house_hyphen and road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase3_house_plot_variation",
            f'"{house_hyphen}" "{road_hyphen}" {location_phrase} company OR business',
        )
    if block_hyphen and plot_hyphen and road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase3_block_plot_road",
            f'"{block_hyphen}" "{plot_hyphen}" "{road_hyphen}" {location_phrase}',
        )

    # Phase 4: Document hunting (operator-first dorks).
    country_tld = _country_to_gl(clean_country)
    _append_unique_attempt_query(
        attempt_queries,
        seen_queries,
        "phase4_country_registry_docs",
        f'site:.{country_tld} "{v_primary}" "{(locality or city or clean_country)}"',
    )
    if plot_hyphen or road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase4_company_plot_road_docs",
            (
                f'filetype:pdf "{v_primary}" '
                f'"{plot_hyphen or plot_space or plot_raw}" OR "{road_hyphen or road_space or road_raw}"'
            ),
        )
    if plot_hyphen and road_hyphen:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase4_address_directory_docs",
            f'filetype:pdf "{plot_hyphen}" "{road_hyphen}" {(locality or city or clean_country)} directory',
        )

    if clean_industry:
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "industry_fallback",
            build_industry_fallback_query(clean_company, clean_industry),
        )

    return attempt_queries[: max(1, max_queries)]


def _normalize_location_token(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    text = re.sub(r"^[,;:\-]+|[,;:\-]+$", "", text).strip()
    return text


def _dedupe_location_parts(parts: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = _normalize_location_token(part)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _is_suspicious_city_state_value(value: str) -> bool:
    lowered = (value or "").lower()
    suspicious_keywords = (
        "block",
        "plot",
        "road",
        "street",
        "st.",
        "sector",
        "house",
        "building",
        "floor",
        "apt",
        "apartment",
    )
    return any(keyword in lowered for keyword in suspicious_keywords)


def _heuristic_city_state_from_full_address(full_address: str) -> tuple[str, str]:
    raw_parts = [part.strip() for part in re.split(r",|\|", full_address or "") if part.strip()]
    cleaned_parts: list[str] = []
    for part in raw_parts:
        # Drop portions heavily numeric (plot numbers, pin codes, etc).
        letters = re.sub(r"[^A-Za-z]", "", part)
        if len(letters) < 2:
            continue
        cleaned_parts.append(part)
    deduped = _dedupe_location_parts(cleaned_parts)
    if not deduped:
        return "", ""
    city = deduped[-1]
    state = deduped[-2] if len(deduped) >= 2 else ""
    if city.lower() == state.lower():
        state = ""
    return city, state


def _address_evidence_markers(full_address: str, max_markers: int = 5) -> list[str]:
    markers: list[str] = []
    if not full_address:
        return markers

    generic_parts = {"dhaka", "bangladesh", "city", "district", "road", "plot", "block", "house"}
    for part in [p.strip() for p in re.split(r",|\|", full_address) if p and p.strip()]:
        normalized = re.sub(r"[^a-z0-9]+", " ", part.lower()).strip()
        normalized = re.sub(r"\brd\b", "road", normalized)
        normalized = re.sub(r"\bst\b", "street", normalized)
        normalized = re.sub(r"\bave\b", "avenue", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if len(normalized) < 3:
            continue
        if normalized in generic_parts:
            continue
        # Postal-only numbers are weak evidence and create false positives.
        if re.fullmatch(r"\d{3,6}", normalized):
            continue
        has_digit = bool(re.search(r"\d", normalized))
        has_letter = bool(re.search(r"[a-z]", normalized))
        if has_digit or (has_letter and len(normalized) >= 6):
            markers.append(normalized)
        if len(markers) >= max_markers:
            break
    return markers


def _normalize_address_match_text(value: Optional[str]) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    normalized = re.sub(r"\brd\b", "road", normalized)
    normalized = re.sub(r"\bst\b", "street", normalized)
    normalized = re.sub(r"\bave\b", "avenue", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _marker_variants_for_match(marker: str) -> set[str]:
    base = _normalize_address_match_text(marker)
    if not base:
        return set()

    variants = {base}
    if "road" in base:
        variants.add(re.sub(r"\broad\b", "rd", base))
    if "rd" in base:
        variants.add(re.sub(r"\brd\b", "road", base))
    if "plot" in base:
        variants.add(re.sub(r"\bplot\b", "house", base))
    if "house" in base:
        variants.add(re.sub(r"\bhouse\b", "plot", base))
    return {re.sub(r"\s+", " ", value).strip() for value in variants if value and value.strip()}


def _marker_matches_candidate(marker: str, normalized_candidate: str) -> bool:
    if not marker or not normalized_candidate:
        return False
    variants = _marker_variants_for_match(marker)
    return any(variant in normalized_candidate for variant in variants)


def _is_address_aligned(input_full_address: str, candidate_address: Optional[str]) -> bool:
    if not input_full_address:
        return True
    if not candidate_address:
        return False
    markers = _address_evidence_markers(input_full_address)
    if not markers:
        return True
    normalized_candidate = _normalize_address_match_text(candidate_address)

    strong_keywords = ("plot", "house", "road", "street", "block", "sector", "building", "tower", "avenue")
    strong_markers = [
        marker
        for marker in markers
        if any(keyword in marker for keyword in strong_keywords)
        or (bool(re.search(r"[a-z]", marker)) and bool(re.search(r"\d", marker)))
    ]
    weak_markers = [marker for marker in markers if marker not in strong_markers]

    if strong_markers:
        return any(_marker_matches_candidate(marker, normalized_candidate) for marker in strong_markers)
    return any(_marker_matches_candidate(marker, normalized_candidate) for marker in weak_markers)


def _meaningful_company_tokens(company_name: str) -> list[str]:
    generic = {
        "company",
        "corporation",
        "limited",
        "ltd",
        "group",
        "trading",
        "co",
        "inc",
        "llc",
        "plc",
        "spa",
        "srl",
        "sp",
        "zoo",
        "z",
        "o",
        "oo",
    }
    return [
        token
        for token in re.findall(r"[a-z0-9]+", (company_name or "").lower())
        if len(token) >= 3 and token not in generic
    ]


def _extract_address_numbers(value: str) -> list[str]:
    numbers = list(dict.fromkeys(re.findall(r"\d{1,5}", value or "")))
    if not numbers:
        return numbers
    short_numbers = [num for num in numbers if len(num) <= 3]
    if short_numbers:
        # When specific short markers exist (plot/road/house numbers), de-prioritize postal codes.
        return short_numbers
    return numbers


def _select_best_gmaps_website(
    gmaps_result: dict[str, Any],
    company_name: str,
    input_full_address: Optional[str],
) -> Optional[str]:
    results = (gmaps_result or {}).get("results") or []
    if not isinstance(results, list):
        return None

    company_tokens = _meaningful_company_tokens(company_name)
    input_address = (input_full_address or "").strip()
    address_markers = _address_evidence_markers(input_address, max_markers=8) if input_address else []
    address_numbers = _extract_address_numbers(input_address) if input_address else []
    company_text = re.sub(r"[^a-z0-9]+", " ", (company_name or "").lower()).strip()

    best_url: Optional[str] = None
    best_score = float("-inf")

    for idx, item in enumerate(results):
        if not isinstance(item, dict):
            continue
        raw_url = str(item.get("website") or "").strip()
        if not raw_url:
            continue
        candidate_url = re.sub(r"#.*$", "", raw_url).strip()
        if is_disallowed_official_url(candidate_url):
            continue

        title = str(item.get("title") or item.get("name") or "")
        category = str(item.get("type") or item.get("category") or "")
        address = str(item.get("address") or "")

        title_norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        category_norm = re.sub(r"[^a-z0-9]+", " ", category.lower()).strip()
        address_norm = _normalize_address_match_text(address)

        score = 0.0

        if company_tokens:
            score += sum(2.0 for token in company_tokens if token in title_norm)

        if address_norm and address_numbers:
            score += sum(3.0 for number in address_numbers if re.search(rf"\b{re.escape(number)}\b", address_norm))

        if address_norm and address_markers:
            score += sum(2.0 for marker in address_markers if _marker_matches_candidate(marker, address_norm))

        if input_address and address:
            if _is_address_aligned(input_address, address):
                score += 2.0
            else:
                score -= 2.0

        organizational_terms = (
            "chamber",
            "embassy",
            "consular",
            "center",
            "centre",
            "ministry",
            "association",
            "council",
            "university",
            "college",
            "school",
            "hospital",
        )
        if any(term in f"{title_norm} {category_norm}" for term in organizational_terms) and not any(
            term in company_text for term in organizational_terms
        ):
            score -= 4.0

        score -= (idx * 0.01)

        if score > best_score:
            best_score = score
            best_url = candidate_url

    return best_url


def is_disallowed_official_url(url: Optional[str]) -> bool:
    if not url:
        return True

    value = url.strip().lower()
    if not value.startswith(("http://", "https://")):
        return True

    parsed = urlparse(value)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if "google." in host or host.endswith(".google"):
        return True

    blocked_domains = (
        "gstatic.com",
        "youtube.com",
        "linkedin.com",
        "facebook.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "wikipedia.org",
        "zoominfo.com",
        "crunchbase.com",
        "bloomberg.com",
        "dnb.com",
        "dandb.com",
        "rocketreach.co",
        "volza.com",
        "opencorporates.com",
        "zaubacorp.com",
        "bangladeshyp.com",
        "globalsuppliersonline.com",
        "eximpedia.app",
        "go4worldbusiness.com",
        "yellowpages.com",
        "yelp.com",
        "manta.com",
        "ecohubmap.com",
        "infobel.ba",
        "biz-gid.com",
        "exportgenius.in",
        "scribd.com",
    )
    if any(host == domain or host.endswith(f".{domain}") for domain in blocked_domains):
        return True

    # Reject direct file/document URLs as official websites.
    file_extensions = (
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".xlsm",
        ".csv",
        ".ppt",
        ".pptx",
        ".pps",
        ".ppsx",
        ".odt",
        ".ods",
        ".odp",
        ".rtf",
        ".txt",
        ".zip",
        ".rar",
        ".7z",
    )
    path = (parsed.path or "").lower()
    disallowed_path_markers = (
        "/document/",
        "/document",
        "/searchviewer/",
        "/snmp/",
        "/wp-content/uploads/",
        "/public/storage/upload/",
    )
    if any(marker in path for marker in disallowed_path_markers):
        return True
    if any(path.endswith(ext) for ext in file_extensions):
        return True

    # Some sites expose downloadable files via query params.
    query = (parsed.query or "").lower()
    query_file_markers = ("file=", "filename=", "download=", "format=pdf", "export=pdf")
    if any(marker in query for marker in query_file_markers):
        for ext in file_extensions:
            if ext in query:
                return True

    return False


def _parse_json_from_text(raw_text: str) -> Optional[dict[str, Any]]:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = cleaned.rstrip("`").strip()

    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def calculate_gemini_cost_usd(usage: Optional[dict[str, Any]]) -> float:
    if not usage:
        return 0.0

    input_tokens = int(usage.get("promptTokenCount", 0) or 0)
    output_tokens = int(usage.get("candidatesTokenCount", 0) or 0)

    input_usd_per_1m = _get_float_env("GEMINI_INPUT_USD_PER_1M_TOKENS", 0.10)
    output_usd_per_1m = _get_float_env("GEMINI_OUTPUT_USD_PER_1M_TOKENS", 0.40)

    cost = ((input_tokens / 1_000_000) * input_usd_per_1m) + (
        (output_tokens / 1_000_000) * output_usd_per_1m
    )
    return round(cost, 8)


def calculate_gemini_batch_cost_usd(usage: Optional[dict[str, Any]]) -> float:
    if not usage:
        return 0.0

    input_tokens = int(usage.get("promptTokenCount", 0) or 0)
    output_tokens = int(usage.get("candidatesTokenCount", 0) or 0)

    input_usd_per_1m = _get_float_env(
        "GEMINI_BATCH_INPUT_USD_PER_1M_TOKENS",
        _get_float_env("GEMINI_INPUT_USD_PER_1M_TOKENS", 0.10),
    )
    output_usd_per_1m = _get_float_env(
        "GEMINI_BATCH_OUTPUT_USD_PER_1M_TOKENS",
        _get_float_env("GEMINI_OUTPUT_USD_PER_1M_TOKENS", 0.40),
    )

    cost = ((input_tokens / 1_000_000) * input_usd_per_1m) + (
        (output_tokens / 1_000_000) * output_usd_per_1m
    )
    return round(cost, 8)


def calculate_serpwow_cost_usd(requests: int) -> float:
    if requests <= 0:
        return 0.0
    usd_per_request = _get_float_env("SERPWOW_USD_PER_REQUEST", 0.0)
    return round(requests * usd_per_request, 8)


def analyze_with_gemini(
    company_name: str,
    country: str,
    search_url: str,
    markdown: str,
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str], Optional[dict[str, Any]]]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY not configured", None, None

    configured_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    model_candidates = [configured_model, "gemini-2.5-flash-lite"]
    seen_models = set()
    ordered_models = []
    for model_name in model_candidates:
        if model_name and model_name not in seen_models:
            seen_models.add(model_name)
            ordered_models.append(model_name)

    prompt = (
        "You are an information extraction system.\\n"
        "Given crawl/search context, return strict JSON only with this schema:\\n"
        "{\\n"
        '  "official_website": string|null,\\n'
        '  "summary": string,\\n'
        '  "confidence": "high"|"medium"|"low",\\n'
        '  "evidence": [string]\\n'
        "}\\n"
        "Rules:\\n"
        "- official_website must be the most likely official company website URL.\\n"
        "- If uncertain, set official_website to null.\\n"
        "- summary must be short and factual.\\n"
        "- evidence must include short source snippets/URLs from the provided text only.\\n\\n"
        f"Company: {company_name}\\n"
        f"Country: {country}\\n"
        f"Search URL: {search_url}\\n\\n"
        "Crawl Markdown:\\n"
        f"{markdown[:12000]}"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    last_error: Optional[str] = None
    for model in ordered_models:
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        req = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(req, timeout=45) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            last_error = f"Gemini HTTPError: {exc.code}"
            if exc.code == 404:
                continue
            return None, last_error, model, None
        except URLError as exc:
            return None, f"Gemini URLError: {exc.reason}", model, None
        except Exception as exc:
            return None, f"Gemini error: {str(exc)}", model, None

        try:
            response_json = json.loads(body)
            text = (
                response_json.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            usage_metadata = response_json.get("usageMetadata", {})
        except Exception:
            return None, "Gemini response parse error", model, None

        parsed = _parse_json_from_text(text)
        if not parsed:
            return None, "Gemini returned non-JSON output", model, usage_metadata

        return parsed, None, model, usage_metadata

    return None, (last_error or "Gemini model resolution failed"), configured_model, None


def standardize_serpwow_ai_overview_with_gemini(
    company_name: str,
    country: str,
    official_website: str,
    ai_overview: dict[str, Any],
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str], Optional[dict[str, Any]]]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY not configured", None, None

    configured_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    model_candidates = [configured_model, "gemini-2.5-flash-lite"]
    seen_models = set()
    ordered_models = []
    for model_name in model_candidates:
        if model_name and model_name not in seen_models:
            seen_models.add(model_name)
            ordered_models.append(model_name)

    prompt = (
        "You are a data normalization system.\n"
        "Convert the provided SerpWow AI Overview into strict JSON only.\n"
        "Schema:\n"
        "{\n"
        '  "address": string|null,\n'
        '  "phone": string|null,\n'
        '  "email": string|null,\n'
        '  "industry": string|null,\n'
        '  "products": [string],\n'
        '  "services": [string]\n'
        "}\n"
        "Rules:\n"
        "- Use only provided input text.\n"
        "- Do not invent values.\n"
        "- Keep values concise.\n"
        "- If not clearly present, return null for scalar fields and [] for lists.\n\n"
        f"Company: {company_name}\n"
        f"Country: {country}\n"
        f"Official Website: {official_website}\n\n"
        "SerpWow AI Overview JSON:\n"
        f"{json.dumps(ai_overview, ensure_ascii=True)[:14000]}"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
        },
    }

    last_error: Optional[str] = None
    for model in ordered_models:
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        req = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(req, timeout=45) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            last_error = f"Gemini HTTPError: {exc.code}"
            if exc.code == 404:
                continue
            return None, last_error, model, None
        except URLError as exc:
            return None, f"Gemini URLError: {exc.reason}", model, None
        except Exception as exc:
            return None, f"Gemini error: {str(exc)}", model, None

        try:
            response_json = json.loads(body)
            text = (
                response_json.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            usage_metadata = response_json.get("usageMetadata", {})
        except Exception:
            return None, "Gemini response parse error", model, None

        parsed = _parse_json_from_text(text)
        if not parsed:
            return None, "Gemini returned non-JSON output", model, usage_metadata

        normalized = {
            "address": parsed.get("address"),
            "phone": parsed.get("phone"),
            "email": parsed.get("email"),
            "industry": parsed.get("industry"),
            "products": parsed.get("products") if isinstance(parsed.get("products"), list) else [],
            "services": parsed.get("services") if isinstance(parsed.get("services"), list) else [],
        }
        return normalized, None, model, usage_metadata

    return None, (last_error or "Gemini model resolution failed"), configured_model, None


def parse_city_state_from_full_address_with_gemini(
    full_address: str,
    country: str,
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str], Optional[dict[str, Any]]]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY not configured", None, None

    configured_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    model_candidates = [configured_model, "gemini-2.5-flash-lite"]
    seen_models = set()
    ordered_models = []
    for model_name in model_candidates:
        if model_name and model_name not in seen_models:
            seen_models.add(model_name)
            ordered_models.append(model_name)

    prompt = (
        "You are an address parser.\n"
        "Extract city and state/province from the address.\n"
        "Return strict JSON only with this schema:\n"
        "{\n"
        '  "city": string|null,\n'
        '  "state": string|null,\n'
        '  "confidence": "high"|"medium"|"low",\n'
        '  "reason": string\n'
        "}\n"
        "Rules:\n"
        "- Use only the provided address text.\n"
        "- Do not invent values.\n"
        "- If unknown, return null.\n\n"
        f"Country hint: {country}\n"
        f"Full address: {full_address}\n"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
        },
    }

    last_error: Optional[str] = None
    for model in ordered_models:
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        req = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(req, timeout=45) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            last_error = f"Gemini HTTPError: {exc.code}"
            if exc.code == 404:
                continue
            return None, last_error, model, None
        except URLError as exc:
            return None, f"Gemini URLError: {exc.reason}", model, None
        except Exception as exc:
            return None, f"Gemini error: {str(exc)}", model, None

        try:
            response_json = json.loads(body)
            text = (
                response_json.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            usage_metadata = response_json.get("usageMetadata", {})
        except Exception:
            return None, "Gemini response parse error", model, None

        parsed = _parse_json_from_text(text)
        if not parsed:
            return None, "Gemini returned non-JSON output", model, usage_metadata

        normalized = {
            "city": (parsed.get("city") or None),
            "state": (parsed.get("state") or None),
            "confidence": parsed.get("confidence"),
            "reason": parsed.get("reason"),
        }
        return normalized, None, model, usage_metadata

    return None, (last_error or "Gemini model resolution failed"), configured_model, None


def transliterate_inputs_with_gemini(
    company_name: str,
    full_address: Optional[str],
    country: Optional[str],
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str], Optional[dict[str, Any]]]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY not configured", None, None

    configured_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    model_candidates = [configured_model, "gemini-2.5-flash-lite"]
    seen_models = set()
    ordered_models = []
    for model_name in model_candidates:
        if model_name and model_name not in seen_models:
            seen_models.add(model_name)
            ordered_models.append(model_name)

    prompt = (
        "You are a text transliteration system.\n"
        "Convert the given company name and full address into Latin script (English letters) only.\n"
        "Keep meaning and pronunciation as close as possible.\n"
        "Return strict JSON only with this schema:\n"
        "{\n"
        '  "company_name_transliterated": string,\n'
        '  "full_address_transliterated": string|null,\n'
        '  "notes": string\n'
        "}\n"
        "Rules:\n"
        "- Do not translate semantics unless needed for script conversion.\n"
        "- Preserve numbers and punctuation when useful.\n"
        "- If text is already in Latin script, return it unchanged.\n"
        "- If full address is empty, return null.\n\n"
        f"Country hint: {country or ''}\n"
        f"Company name: {company_name}\n"
        f"Full address: {full_address or ''}\n"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
        },
    }

    last_error: Optional[str] = None
    for model in ordered_models:
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        req = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(req, timeout=45) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            last_error = f"Gemini HTTPError: {exc.code}"
            if exc.code == 404:
                continue
            return None, last_error, model, None
        except URLError as exc:
            return None, f"Gemini URLError: {exc.reason}", model, None
        except Exception as exc:
            return None, f"Gemini error: {str(exc)}", model, None

        try:
            response_json = json.loads(body)
            text = (
                response_json.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            usage_metadata = response_json.get("usageMetadata", {})
        except Exception:
            return None, "Gemini response parse error", model, None

        parsed = _parse_json_from_text(text)
        if not parsed:
            return None, "Gemini returned non-JSON output", model, usage_metadata

        normalized = {
            "company_name_transliterated": str(parsed.get("company_name_transliterated") or "").strip(),
            "full_address_transliterated": (
                str(parsed.get("full_address_transliterated")).strip()
                if parsed.get("full_address_transliterated") is not None
                else None
            ),
            "notes": str(parsed.get("notes") or "").strip(),
        }
        return normalized, None, model, usage_metadata

    return None, (last_error or "Gemini model resolution failed"), configured_model, None


def classify_address_with_gemini(
    full_address: Optional[str],
    country: Optional[str],
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str], Optional[dict[str, Any]]]:
    address_text = str(full_address or "").strip()
    if not address_text:
        return None, "Skipped because full_address was unavailable.", None, None

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY not configured", None, None

    configured_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    model_candidates = [configured_model, "gemini-2.5-flash-lite"]
    seen_models = set()
    ordered_models = []
    for model_name in model_candidates:
        if model_name and model_name not in seen_models:
            seen_models.add(model_name)
            ordered_models.append(model_name)

    prompt = (
        "You are an address classification system.\n"
        "Given a country and full address, classify formatting quality and address type.\n"
        "Return strict JSON only with this schema:\n"
        "{\n"
        '  "country_format_validity": "valid"|"likely_valid"|"invalid"|"uncertain",\n'
        '  "is_complete": boolean,\n'
        '  "completeness_level": "complete"|"partial"|"insufficient",\n'
        '  "address_type": "mailbox"|"campus"|"building"|"mall"|"office"|"industrial"|"residential"|"landmark"|"mixed"|"unknown",\n'
        '  "reason": string\n'
        "}\n"
        "Rules:\n"
        "- Use country-specific conventions as best effort.\n"
        "- is_complete should be true only when major components are present for that country.\n"
        "- If unsure, use uncertain/unknown and explain in reason.\n\n"
        f"Country: {country or ''}\n"
        f"Full address: {address_text}\n"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
        },
    }

    last_error: Optional[str] = None
    for model in ordered_models:
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        req = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(req, timeout=45) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            last_error = f"Gemini HTTPError: {exc.code}"
            if exc.code == 404:
                continue
            return None, last_error, model, None
        except URLError as exc:
            return None, f"Gemini URLError: {exc.reason}", model, None
        except Exception as exc:
            return None, f"Gemini error: {str(exc)}", model, None

        try:
            response_json = json.loads(body)
            text = (
                response_json.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            usage_metadata = response_json.get("usageMetadata", {})
        except Exception:
            return None, "Gemini response parse error", model, None

        parsed = _parse_json_from_text(text)
        if not parsed:
            return None, "Gemini returned non-JSON output", model, usage_metadata

        normalized = {
            "country_format_validity": str(parsed.get("country_format_validity") or "uncertain").strip().lower(),
            "is_complete": bool(parsed.get("is_complete")),
            "completeness_level": str(parsed.get("completeness_level") or "insufficient").strip().lower(),
            "address_type": str(parsed.get("address_type") or "unknown").strip().lower(),
            "reason": str(parsed.get("reason") or "").strip(),
        }
        return normalized, None, model, usage_metadata

    return None, (last_error or "Gemini model resolution failed"), configured_model, None


def choose_final_website_with_gemini(
    company_name: str,
    country: str,
    input_industry: Optional[str],
    input_full_address: Optional[str],
    candidate_urls: list[str],
    search_attempts: list[dict[str, Any]],
    search_raw: Optional[dict[str, Any]],
    gmaps_context: dict[str, Any],
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str], Optional[dict[str, Any]]]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY not configured", None, None

    configured_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    model_candidates = [configured_model, "gemini-2.5-flash-lite"]
    seen_models = set()
    ordered_models = []
    for model_name in model_candidates:
        if model_name and model_name not in seen_models:
            seen_models.add(model_name)
            ordered_models.append(model_name)

    search_summary: dict[str, Any] = {
        "knowledge_graph_website": ((search_raw or {}).get("knowledge_graph") or {}).get("website")
        if isinstance(search_raw, dict)
        else None,
        "answer_box_link": ((search_raw or {}).get("answer_box") or {}).get("link")
        if isinstance(search_raw, dict)
        else None,
        "top_organic_links": [
            {
                "title": item.get("title"),
                "link": item.get("link") or item.get("url"),
            }
            for item in (((search_raw or {}).get("organic_results") or [])[:8] if isinstance(search_raw, dict) else [])
            if isinstance(item, dict)
        ],
    }
    gmaps_raw = gmaps_context.get("raw_response")
    gmaps_summary: dict[str, Any] = {
        "query": gmaps_context.get("query"),
        "official_website": gmaps_context.get("official_website"),
        "top_results": [
            {
                "title": item.get("title"),
                "name": item.get("name"),
                "website": item.get("website"),
                "address": item.get("address"),
            }
            for item in (((gmaps_raw or {}).get("results") or [])[:8] if isinstance(gmaps_raw, dict) else [])
            if isinstance(item, dict)
        ],
    }

    normalized_candidates = [_normalize_url_for_compare(u) for u in candidate_urls if str(u or "").strip()]
    normalized_candidates = [u for u in normalized_candidates if u]
    candidate_set = set(normalized_candidates)

    prompt = (
        "You are a domain validation and confidence scoring system.\n"
        "Select the most likely official website URL for the target company using all provided evidence.\n"
        "Return strict JSON only with this schema:\n"
        "{\n"
        '  "official_website": string|null,\n'
        '  "confidence_score": number,\n'
        '  "confidence": "high"|"medium"|"low",\n'
        '  "reason": string,\n'
        '  "evidence": [string],\n'
        '  "alternatives": [string]\n'
        "}\n"
        "Rules:\n"
        "- official_website MUST be one of candidate_urls exactly, or null.\n"
        "- Do not invent URLs and do not select URLs outside candidate_urls.\n"
        "- Never select directory/listing/data-broker/profile domains (e.g., zoominfo, crunchbase, yellowpages, business directories).\n"
        "- If Input Full Address is present, prefer URLs tied to that same address/locality and penalize candidates with clearly conflicting addresses.\n"
        "- If uncertain, set official_website to null.\n"
        "- confidence_score must be 0-100.\n"
        "- confidence must match confidence_score: high>=80, medium 50-79, low<50.\n"
        "- evidence should reference specific provided signals only.\n\n"
        f"Company: {company_name}\n"
        f"Country: {country}\n"
        f"Input Industry: {(input_industry or '').strip()}\n"
        f"Input Full Address: {(input_full_address or '').strip()}\n\n"
        f"Candidate URLs: {json.dumps(normalized_candidates, ensure_ascii=True)}\n\n"
        f"Search Attempts: {json.dumps(search_attempts, ensure_ascii=True)[:6000]}\n\n"
        f"SerpWow Search Summary: {json.dumps(search_summary, ensure_ascii=True)[:5000]}\n\n"
        f"GMaps Summary: {json.dumps(gmaps_summary, ensure_ascii=True)[:5000]}"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    last_error: Optional[str] = None
    for model in ordered_models:
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        req = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(req, timeout=45) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            last_error = f"Gemini HTTPError: {exc.code}"
            if exc.code == 404:
                continue
            return None, last_error, model, None
        except URLError as exc:
            return None, f"Gemini URLError: {exc.reason}", model, None
        except Exception as exc:
            return None, f"Gemini error: {str(exc)}", model, None

        try:
            response_json = json.loads(body)
            text = (
                response_json.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            usage_metadata = response_json.get("usageMetadata", {})
        except Exception:
            return None, "Gemini response parse error", model, None

        parsed = _parse_json_from_text(text)
        if not parsed:
            return None, "Gemini returned non-JSON output", model, usage_metadata

        confidence_score_raw = parsed.get("confidence_score", 0)
        try:
            confidence_score = int(float(confidence_score_raw))
        except Exception:
            confidence_score = 0
        confidence_score = max(0, min(100, confidence_score))

        normalized = {
            "official_website": parsed.get("official_website"),
            "confidence_score": confidence_score,
            "confidence": parsed.get("confidence"),
            "reason": parsed.get("reason"),
            "evidence": parsed.get("evidence") if isinstance(parsed.get("evidence"), list) else [],
            "alternatives": parsed.get("alternatives") if isinstance(parsed.get("alternatives"), list) else [],
        }
        selected = normalized.get("official_website")
        if isinstance(selected, str) and selected.strip():
            selected_norm = _normalize_url_for_compare(selected)
            if not selected_norm or selected_norm not in candidate_set:
                normalized["official_website"] = None
                normalized["confidence_score"] = 0
                normalized["confidence"] = "low"
                normalized["reason"] = (
                    f"Rejected Gemini-selected URL outside candidate set: {selected_norm or selected.strip()}"
                )
            elif not _official_website_looks_plausible(selected_norm, company_name, country):
                normalized["official_website"] = None
                normalized["confidence_score"] = 0
                normalized["confidence"] = "low"
                normalized["reason"] = (
                    f"Rejected Gemini-selected URL due to low company-domain plausibility: {selected_norm}"
                )
            else:
                normalized["official_website"] = selected_norm
        return normalized, None, model, usage_metadata

    return None, (last_error or "Gemini model resolution failed"), configured_model, None


def _domain_from_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    host = (parsed.netloc or parsed.path or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split("/")[0]


async def run_serpwow_from_codetails(official_website: str, country: Optional[str] = None) -> dict[str, Any]:
    domain = _domain_from_url(official_website)
    if not domain:
        return {
            "provider": "serpwow",
            "used": False,
            "domain": None,
            "query": None,
            "request_count": 0,
            "ai_overview": None,
            "raw_response": None,
            "error": "Could not extract domain from official website URL",
        }

    try:
        from app.services.serpwow import codetails as codetails_module
    except Exception as exc:
        return {
            "provider": "serpwow",
            "used": False,
            "domain": domain,
            "query": None,
            "request_count": 0,
            "ai_overview": None,
            "raw_response": None,
            "error": f"Failed to import codetails.py: {str(exc)}",
        }

    try:
        request_count = 0
        query = (
            codetails_module.build_query(domain)
            if hasattr(codetails_module, "build_query")
            else f"What is the address, phone, email, industry, products, services of {domain}"
        )
        gl = _country_to_gl(country)
        raw_data = await asyncio.to_thread(codetails_module.fetch_serpwow, query, gl)
        request_count += 1
        ai_overview = (
            codetails_module.get_ai_overview(raw_data)
            if hasattr(codetails_module, "get_ai_overview")
            else raw_data.get("ai_overview", {})
        )

        is_placeholder = (
            bool(codetails_module.ai_overview_is_placeholder(ai_overview))
            if hasattr(codetails_module, "ai_overview_is_placeholder")
            else False
        )
        if is_placeholder and hasattr(codetails_module, "build_fallback_query"):
            fallback_query = codetails_module.build_fallback_query(domain)
            fallback_data = await asyncio.to_thread(
                codetails_module.fetch_serpwow, fallback_query, gl
            )
            request_count += 1
            fallback_ai = (
                codetails_module.get_ai_overview(fallback_data)
                if hasattr(codetails_module, "get_ai_overview")
                else fallback_data.get("ai_overview", {})
            )
            if not codetails_module.ai_overview_is_placeholder(fallback_ai):
                raw_data = fallback_data
                ai_overview = fallback_ai
                query = fallback_query

        return {
            "provider": "serpwow",
            "used": True,
            "domain": domain,
            "query": query,
            "request_count": request_count,
            "ai_overview": ai_overview,
            "raw_response": raw_data,
            "error": None,
        }
    except Exception as exc:
        return {
            "provider": "serpwow",
            "used": False,
            "domain": domain,
            "query": None,
            "request_count": 0,
            "ai_overview": None,
            "raw_response": None,
            "error": str(exc),
        }


async def run_gmaps_from_module(
    company_name: str,
    country: str,
    input_industry: Optional[str] = None,
    input_full_address: Optional[str] = None,
) -> dict[str, Any]:
    try:
        from app.services.serpwow import gmaps as gmaps_module
    except Exception as exc:
        return {
            "provider": "gmaps",
            "used": False,
            "query": None,
            "official_website": None,
            "request_count": 0,
            "raw_response": None,
            "error": f"Failed to import gmaps.py: {str(exc)}",
        }

    try:
        location_hint = (input_full_address or "").strip() or (country or "").strip()
        query = " ".join(part for part in [company_name or "", location_hint] if part).strip()
        if not query:
            query = (company_name or "").strip()

        gmaps_result = await gmaps_module.process_gmaps_query(query, country=_country_to_gl(country))
        gmaps_website = _select_best_gmaps_website(
            gmaps_result,
            company_name=company_name,
            input_full_address=input_full_address,
        )
        if not gmaps_website:
            gmaps_website = (
                gmaps_module.extract_gmaps_website(gmaps_result)
                if hasattr(gmaps_module, "extract_gmaps_website")
                else None
            )
        if is_disallowed_official_url(gmaps_website):
            gmaps_website = None

        request_count = int((gmaps_result or {}).get("request_count", 0) or 0)
        if request_count < 0:
            request_count = 0

        return {
            "provider": "gmaps",
            "used": True,
            "query": query,
            "official_website": gmaps_website,
            "request_count": request_count,
            "raw_response": gmaps_result,
            "error": None,
        }
    except Exception as exc:
        return {
            "provider": "gmaps",
            "used": False,
            "query": None,
            "official_website": None,
            "request_count": 0,
            "raw_response": None,
            "error": str(exc),
        }


def _normalize_website_input(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    host = (parsed.netloc or parsed.path or "").strip()
    if not host:
        return ""
    path = parsed.path if parsed.netloc else ""
    normalized = f"{parsed.scheme or 'https'}://{host}{path}"
    return normalized.rstrip("/")


async def execute_gsearch_lookup_for_worker(
    company_name: str,
    country: str,
    firm_id: Optional[str] = None,
    input_industry: Optional[str] = None,
    input_full_address: Optional[str] = None,
    debug_upload_id: Optional[str] = None,
    debug_row_index: Optional[int] = None,
    phase: str = "all",
) -> tuple[CrawlResponse, str]:
    queries = build_selected_phase_queries(
        company_name=company_name,
        country=country,
        parsed_city_state="",
        full_address=input_full_address or "",
        industry=input_industry or "",
        phase=phase,
    )
    
    if phase == "phase5":
        phase1_queries = build_selected_phase_queries(
            company_name=company_name,
            country=country,
            parsed_city_state="",
            full_address=input_full_address or "",
            industry=input_industry or "",
            phase="phase1",
        )
        target_people = []
        target_trade_names = []
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
        
        plot_raw = _extract_address_component(input_full_address, ("plot",)) if input_full_address else ""
        road_raw = _extract_address_component(input_full_address, ("road", " rd")) if input_full_address else ""
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
    
    serpwow_cost = 0.0
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
            }
        
        serpwow_cost += 0.02
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
            "search_url": raw_result.get("search_url"),
            "raw_response": raw_result.get("raw_response"),
        })
        
    best_candidate = candidates[0] if candidates else None
    
    summary_text = (
        f"Modular Search Phase: {phase} completed. "
        f"Executed {len(queries)} query variations. "
        f"Found {len(candidates)} unique candidates."
    )
    
    crawl_resp = CrawlResponse(
        company_name=company_name,
        country=country,
        firm_id=firm_id,
        input_industry=input_industry,
        input_full_address=input_full_address,
        official_website=best_candidate,
        summary=summary_text,
        address=input_full_address,
        phone=None,
        email=None,
        industry=input_industry,
        products=[],
        services=[],
        website_company_descirption_ai=None,
        website_company_descirption_translated_ai=None,
        massive_proxy_cost_usd=0.0,
        serpwow_cost_usd=serpwow_cost,
        gemini_cost_usd=0.0,
        total_cost_usd=serpwow_cost,
        context={
            "pipeline": PIPELINE_GSEARCH,
            "phase": phase,
            "success": bool(best_candidate),
            "used_proxy": False,
            "blocked": False,
            "candidates": candidates,
            "formatted_results": formatted_results,
            "cost_breakdown": {
                "massive_proxy_cost_usd": 0.0,
                "serpwow_cost_usd": serpwow_cost,
                "gemini_cost_usd": 0.0,
                "total_cost_usd": serpwow_cost,
                "serpwow_request_count": len(queries),
            }
        }
    )
    
    unified_raw_serpwow = {
        "queries": queries,
        "candidates": candidates,
        "results": formatted_results,
    }
    
    return crawl_resp, json.dumps(unified_raw_serpwow)


async def execute_gmaps_lookup(
    company_name: str,
    country: str,
    firm_id: Optional[str] = None,
    input_industry: Optional[str] = None,
    input_full_address: Optional[str] = None,
    debug_upload_id: Optional[str] = None,
    debug_row_index: Optional[int] = None,
) -> tuple[CrawlResponse, str]:
    started_monotonic = asyncio.get_event_loop().time()
    
    # Run only Google Maps search using run_gmaps_from_module
    gmaps_context = await run_gmaps_from_module(
        company_name=company_name,
        country=country,
        input_industry=input_industry,
        input_full_address=input_full_address,
    )
    
    gmaps_website = gmaps_context.get("official_website")
    gmaps_result = gmaps_context.get("raw_response") or {}
    
    # Extract details from first result or result if it's a list
    results = gmaps_result.get("results") if isinstance(gmaps_result, dict) else None
    first_place = results[0] if (isinstance(results, list) and len(results) > 0) else gmaps_result
    if not isinstance(first_place, dict):
        first_place = {}
        
    address = first_place.get("address") or first_place.get("formatted_address")
    phone = first_place.get("phone")
    rating = first_place.get("rating")
    reviews = first_place.get("reviews")
    categories = first_place.get("categories") or ([first_place.get("category")] if first_place.get("category") else [])
    
    summary = "Google Maps details lookup successfully resolved." if gmaps_context.get("used") else "Google Maps lookup failed."
    if gmaps_context.get("error"):
        summary = f"Google Maps error: {gmaps_context.get('error')}"
        
    gmaps_requests_used = int(gmaps_context.get("request_count", 0) or 0)
    serpwow_cost_usd = calculate_serpwow_cost_usd(gmaps_requests_used)
    
    # Create CrawlResponse
    response = CrawlResponse(
        company_name=company_name,
        country=country,
        firm_id=firm_id,
        input_industry=input_industry,
        input_full_address=input_full_address,
        official_website=gmaps_website,
        summary=summary,
        address=address,
        phone=phone,
        email=None,
        industry=categories[0] if (categories and len(categories) > 0) else None,
        products=[],
        services=[],
        massive_proxy_cost_usd=0.0,
        serpwow_cost_usd=serpwow_cost_usd,
        gemini_cost_usd=0.0,
        total_cost_usd=serpwow_cost_usd,
        context={
            "pipeline": PIPELINE_GMAPS,
            "success": bool(gmaps_website),
            "used_proxy": False,
            "blocked": False,
            "error": gmaps_context.get("error"),
            "gmaps": gmaps_context,
            "cost_breakdown": {
                "massive_proxy_cost_usd": 0.0,
                "serpwow_cost_usd": serpwow_cost_usd,
                "gemini_cost_usd": 0.0,
                "total_cost_usd": serpwow_cost_usd,
                "serpwow_request_count": gmaps_requests_used,
            }
        }
    )
    
    serpwow_raw_json = json.dumps(gmaps_result, ensure_ascii=True, indent=2)
    return response, serpwow_raw_json


async def execute_firmographic_extraction(
    official_website: str,
    company_name: Optional[str],
    country: Optional[str],
    firm_id: Optional[str] = None,
    input_industry: Optional[str] = None,
    input_full_address: Optional[str] = None,
) -> tuple[CrawlResponse, str]:
    normalized_official = _normalize_website_input(official_website)
    clean_company_name = (company_name or "").strip() or _domain_from_url(normalized_official)
    clean_country = (country or "").strip()
    clean_full_address = (input_full_address or "").strip() or None
    summary = "Firmographic extraction completed from provided official website."

    if not normalized_official or is_disallowed_official_url(normalized_official):
        response = CrawlResponse(
            company_name=clean_company_name,
            country=clean_country,
            firm_id=firm_id,
            input_industry=input_industry,
            input_full_address=input_full_address,
            official_website=None,
            summary="Invalid or unsupported official website for firmographic extraction.",
            address=None,
            phone=None,
            email=None,
            industry=None,
            products=[],
            services=[],
            massive_proxy_cost_usd=0.0,
            serpwow_cost_usd=0.0,
            gemini_cost_usd=0.0,
            total_cost_usd=0.0,
            context={
                "pipeline": PIPELINE_FIRMOGRAPHICS,
                "serpwow": {
                    "provider": "serpwow",
                    "used": False,
                    "domain": None,
                    "query": None,
                    "request_count": 0,
                    "ai_overview": None,
                    "raw_response": None,
                    "error": "Invalid official website input.",
                },
                "serpwow_mapping_ai": {
                    "provider": "google-gemini",
                    "model": None,
                    "used": False,
                    "error": "Skipped because official_website was invalid.",
                    "usage": {},
                    "raw": None,
                },
                "cost_breakdown": {
                    "massive_proxy_cost_usd": 0.0,
                    "serpwow_cost_usd": 0.0,
                    "gemini_cost_usd": 0.0,
                    "total_cost_usd": 0.0,
                    "serpwow_request_count": 0,
                },
            },
        )
        return response, ""

    serpwow_context = await run_serpwow_from_codetails(normalized_official, country=clean_country)
    serpwow_raw_json = (
        json.dumps(serpwow_context.get("raw_response"), ensure_ascii=True, indent=2)
        if serpwow_context.get("raw_response") is not None
        else ""
    )

    mapped_columns = {
        "address": None,
        "phone": None,
        "email": None,
        "industry": None,
        "products": [],
        "services": [],
    }
    serpwow_mapping_ai_context = {
        "provider": "google-gemini",
        "model": None,
        "used": False,
        "error": "Skipped because SerpWow ai_overview was unavailable.",
        "usage": {},
        "raw": None,
    }

    if isinstance(serpwow_context.get("ai_overview"), dict):
        mapped_output, mapped_error, mapped_model, mapped_usage = (
            await asyncio.to_thread(
                standardize_serpwow_ai_overview_with_gemini,
                clean_company_name,
                clean_country,
                normalized_official,
                serpwow_context.get("ai_overview") or {},
            )
        )
        if isinstance(mapped_output, dict):
            mapped_columns = {
                "address": mapped_output.get("address"),
                "phone": mapped_output.get("phone"),
                "email": mapped_output.get("email"),
                "industry": mapped_output.get("industry"),
                "products": mapped_output.get("products") or [],
                "services": mapped_output.get("services") or [],
            }
        serpwow_mapping_ai_context = {
            "provider": "google-gemini",
            "model": mapped_model,
            "used": mapped_output is not None,
            "error": mapped_error,
            "usage": mapped_usage or {},
            "raw": mapped_output,
        }

    if clean_full_address:
        mapped_address_value = mapped_columns.get("address")
        if mapped_address_value and not _is_address_aligned(clean_full_address, mapped_address_value):
            summary = (
                "Firmographics extracted, but mapped address is not aligned with provided input address."
            )

    serpwow_mapping_ai_cost_usd = calculate_gemini_cost_usd(
        serpwow_mapping_ai_context.get("usage")
        if isinstance(serpwow_mapping_ai_context, dict)
        else None
    )
    serpwow_request_count = int(serpwow_context.get("request_count", 0) or 0)
    serpwow_cost_usd = calculate_serpwow_cost_usd(serpwow_request_count)
    gemini_cost_usd = round(serpwow_mapping_ai_cost_usd, 8)
    total_cost_usd = round(serpwow_cost_usd + gemini_cost_usd, 8)

    response = CrawlResponse(
        company_name=clean_company_name,
        country=clean_country,
        firm_id=firm_id,
        input_industry=input_industry,
        input_full_address=input_full_address,
        official_website=normalized_official,
        summary=summary,
        address=mapped_columns.get("address"),
        phone=mapped_columns.get("phone"),
        email=mapped_columns.get("email"),
        industry=mapped_columns.get("industry"),
        products=mapped_columns.get("products") or [],
        services=mapped_columns.get("services") or [],
        massive_proxy_cost_usd=0.0,
        serpwow_cost_usd=serpwow_cost_usd,
        gemini_cost_usd=gemini_cost_usd,
        total_cost_usd=total_cost_usd,
        context={
            "pipeline": PIPELINE_FIRMOGRAPHICS,
            "serpwow": serpwow_context,
            "serpwow_mapping_ai": serpwow_mapping_ai_context,
            "cost_breakdown": {
                "massive_proxy_cost_usd": 0.0,
                "serpwow_cost_usd": serpwow_cost_usd,
                "gemini_cost_usd": gemini_cost_usd,
                "total_cost_usd": total_cost_usd,
                "serpwow_request_count": serpwow_request_count,
            },
        },
    )
    return response, serpwow_raw_json



async def execute_company_lookup(
    company_name: str,
    country: str,
    firm_id: Optional[str] = None,
    input_industry: Optional[str] = None,
    input_full_address: Optional[str] = None,
    debug_upload_id: Optional[str] = None,
    debug_row_index: Optional[int] = None,
    include_firmographics: bool = True,
) -> tuple[CrawlResponse, str]:
    started_monotonic = asyncio.get_event_loop().time()
    original_company_name = company_name
    original_input_full_address = input_full_address
    working_company_name = (company_name or "").strip()
    working_full_address = (input_full_address or "").strip()
    batch_postprocess_for_upload = bool(debug_upload_id) and _get_bool_env(
        "ENABLE_GEMINI_BATCH_POSTPROCESS",
        False,
    )
    _log_row_stage(
        "lookup.start",
        (
            f"company={_short_text(original_company_name, 120)!r} "
            f"country={_short_text(country, 60)!r} "
            f"firm_id={_short_text(firm_id, 40)!r} "
            f"industry_present={bool(input_industry and str(input_industry).strip())} "
            f"address_present={bool(input_full_address and str(input_full_address).strip())}"
        ),
        upload_id=debug_upload_id,
        row_index=debug_row_index,
    )

    transliteration_ai_context: dict[str, Any] = {
        "provider": "google-gemini",
        "model": None,
        "used": False,
        "error": "Skipped because transliteration inputs were unavailable.",
        "usage": {},
        "raw": None,
    }
    if working_company_name or working_full_address:
        translit_output, translit_error, translit_model, translit_usage = (
            await asyncio.to_thread(
                transliterate_inputs_with_gemini,
                working_company_name,
                working_full_address,
                country,
            )
        )
        transliteration_ai_context = {
            "provider": "google-gemini",
            "model": translit_model,
            "used": translit_output is not None,
            "error": translit_error,
            "usage": translit_usage or {},
            "raw": translit_output,
        }
        if isinstance(translit_output, dict):
            translit_company = str(translit_output.get("company_name_transliterated") or "").strip()
            if translit_company:
                working_company_name = translit_company
            translit_address = translit_output.get("full_address_transliterated")
            if isinstance(translit_address, str) and translit_address.strip():
                working_full_address = translit_address.strip()
        _log_row_stage(
            "lookup.transliteration",
            (
                f"used={transliteration_ai_context.get('used')} "
                f"model={_short_text(translit_model, 80)!r} "
                f"error={_short_text(translit_error, 180)!r} "
                f"company_after={_short_text(working_company_name, 120)!r} "
                f"address_after_present={bool(working_full_address)}"
            ),
            upload_id=debug_upload_id,
            row_index=debug_row_index,
        )

    address_classification_ai_context: dict[str, Any] = {
        "provider": "google-gemini",
        "model": None,
        "used": False,
        "error": "Skipped because full_address was unavailable.",
        "usage": {},
        "raw": None,
    }
    if working_full_address:
        class_output, class_error, class_model, class_usage = await asyncio.to_thread(
            classify_address_with_gemini,
            working_full_address,
            country,
        )
        address_classification_ai_context = {
            "provider": "google-gemini",
            "model": class_model,
            "used": class_output is not None,
            "error": class_error,
            "usage": class_usage or {},
            "raw": class_output,
        }
        _log_row_stage(
            "lookup.address_classification",
            (
                f"used={address_classification_ai_context.get('used')} "
                f"model={_short_text(class_model, 80)!r} "
                f"error={_short_text(class_error, 180)!r}"
            ),
            upload_id=debug_upload_id,
            row_index=debug_row_index,
        )

    enable_industry_fallback = _get_bool_env("ENABLE_INDUSTRY_FALLBACK", True)
    search_attempts: list[dict[str, Any]] = []
    attempt_queries: list[tuple[str, str]] = []
    full_address_value = working_full_address
    parsed_city_state_ai_context: dict[str, Any] = {
        "provider": "google-gemini",
        "model": None,
        "used": False,
        "error": "Skipped because full_address was unavailable.",
        "usage": {},
        "raw": None,
    }

    parsed_city_state_text = ""
    if full_address_value:
        parsed_output, parsed_error, parsed_model, parsed_usage = (
            await asyncio.to_thread(
                parse_city_state_from_full_address_with_gemini,
                full_address_value,
                country,
            )
        )
        parsed_city_state_ai_context = {
            "provider": "google-gemini",
            "model": parsed_model,
            "used": parsed_output is not None,
            "error": parsed_error,
            "usage": parsed_usage or {},
            "raw": parsed_output,
        }
        if isinstance(parsed_output, dict):
            parsed_city = _normalize_location_token(str(parsed_output.get("city") or ""))
            parsed_state = _normalize_location_token(str(parsed_output.get("state") or ""))
            parsed_parts = _dedupe_location_parts([parsed_city, parsed_state])
            # Guard against parser returning street-level fragments as "city/state".
            if parsed_parts and _is_suspicious_city_state_value(parsed_parts[0]):
                parsed_parts = []
            if parsed_parts:
                first = parsed_parts[0]
                if _is_suspicious_city_state_value(first):
                    parsed_parts = []
            if not parsed_parts:
                heuristic_city, heuristic_state = _heuristic_city_state_from_full_address(full_address_value)
                parsed_parts = _dedupe_location_parts([heuristic_city, heuristic_state])
            if parsed_parts:
                parsed_city_state_text = " ".join(parsed_parts)
                _log_row_stage(
                    "lookup.city_state_query",
                    (
                        f"query={_short_text(build_address_fallback_query(working_company_name, parsed_city_state_text), 180)!r} "
                        f"city_state={_short_text(' '.join(parsed_parts), 120)!r}"
                    ),
                    upload_id=debug_upload_id,
                    row_index=debug_row_index,
                )
    max_search_attempts = max(1, _get_int_env("SEARCH_MAX_ATTEMPTS", 25))
    attempt_queries = build_investigative_search_queries(
        company_name=working_company_name,
        country=country,
        parsed_city_state=parsed_city_state_text,
        full_address=full_address_value or "",
        industry=(input_industry.strip() if (enable_industry_fallback and input_industry and input_industry.strip()) else ""),
        max_queries=max_search_attempts,
    )
    raw_company_name = (original_company_name or "").strip()
    raw_full_address = (original_input_full_address or "").strip()
    raw_company_norm = _normalize_location_token(raw_company_name).lower()
    raw_address_norm = _normalize_location_token(raw_full_address).lower()
    working_company_norm = _normalize_location_token(working_company_name).lower()
    working_address_norm = _normalize_location_token(working_full_address).lower()
    should_add_raw_script_probe = (
        bool(raw_company_name)
        and (
            raw_company_norm != working_company_norm
            or (bool(raw_full_address) and raw_address_norm != working_address_norm)
        )
    )
    raw_probe_added = False
    if should_add_raw_script_probe:
        raw_probe_candidates = build_investigative_search_queries(
            company_name=raw_company_name,
            country=country,
            parsed_city_state="",
            full_address=raw_full_address,
            industry="",
            max_queries=1,
        )
        if raw_probe_candidates:
            _, raw_probe_query = raw_probe_candidates[0]
            existing_query_keys = {re.sub(r"\s+", " ", query).strip().lower() for _, query in attempt_queries}
            normalized_raw_probe_query = re.sub(r"\s+", " ", raw_probe_query).strip().lower()
            if (
                normalized_raw_probe_query
                and normalized_raw_probe_query not in existing_query_keys
                and len(attempt_queries) < max_search_attempts
            ):
                attempt_queries.append(("phase0_non_transliterated_probe", raw_probe_query))
                raw_probe_added = True
        _log_row_stage(
            "lookup.search_plan_raw_probe",
            (
                f"enabled={should_add_raw_script_probe} "
                f"added={raw_probe_added} "
                f"query={_short_text(raw_probe_candidates[0][1], 180)!r}"
                if raw_probe_candidates
                else f"enabled={should_add_raw_script_probe} added={raw_probe_added} query=None"
            ),
            upload_id=debug_upload_id,
            row_index=debug_row_index,
        )
    _log_row_stage(
        "lookup.search_plan",
        (
            f"attempts={len(attempt_queries)} "
            f"max_attempts={max_search_attempts} "
            f"labels={[label for label, _ in attempt_queries]}"
        ),
        upload_id=debug_upload_id,
        row_index=debug_row_index,
    )

    selected_search_url: Optional[str] = None
    last_status_code: Optional[int] = None
    serpwow_search_requests_used = 0
    candidate_urls: list[str] = []
    search_context: dict[str, Any] = {
        "provider": "serpwow",
        "used": False,
        "error": "No search attempt was executed.",
        "raw": None,
    }
    gmaps_context: dict[str, Any] = {
        "provider": "gmaps",
        "used": False,
        "query": None,
        "official_website": None,
        "request_count": 0,
        "raw_response": None,
        "error": "Skipped because SerpWow search already determines official website.",
    }
    final_url_selection_ai_context: dict[str, Any] = {
        "provider": "google-gemini",
        "model": None,
        "used": False,
        "error": "Skipped because final URL confidence scoring was not needed.",
        "usage": {},
        "raw": None,
    }
    official_website: Optional[str] = None
    summary = "Official website could not be determined from SerpWow search results."
    candidate_verification_added = False
    seen_attempt_query_keys = {re.sub(r"\s+", " ", query).strip().lower() for _, query in attempt_queries}
    phase5_plot_marker, _ = _marker_variants(_extract_address_component(working_full_address, ("plot",)))
    phase5_road_marker, _ = _marker_variants(_extract_address_component(working_full_address, ("road", " rd")))

    phase5_attempt_queries: list[tuple[str, str]] = []

    def _process_serpwow_attempt_result(
        attempt_label: str,
        query: str,
        attempt_result: dict[str, Any],
        allow_phase5_expansion: bool,
    ) -> None:
        nonlocal serpwow_search_requests_used
        nonlocal selected_search_url
        nonlocal last_status_code
        nonlocal official_website
        nonlocal summary
        nonlocal search_context
        nonlocal candidate_verification_added

        serpwow_search_requests_used += 1
        selected_search_url = attempt_result.get("search_url")
        last_status_code = attempt_result.get("status_code")
        attempt_official_website = attempt_result.get("official_website")
        attempt_candidates = (
            attempt_result.get("candidates")
            if isinstance(attempt_result.get("candidates"), list)
            else []
        )
        _log_row_stage(
            "lookup.serpwow_attempt",
            (
                f"attempt={attempt_label} "
                f"status_code={attempt_result.get('status_code')} "
                f"official={_short_text(attempt_official_website, 140)!r} "
                f"candidates={len(attempt_candidates)} "
                f"error={_short_text(attempt_result.get('error'), 200)!r} "
                f"query={_short_text(query, 180)!r}"
            ),
            upload_id=debug_upload_id,
            row_index=debug_row_index,
        )
        if (
            isinstance(attempt_official_website, str)
            and attempt_official_website.strip()
            and not is_disallowed_official_url(attempt_official_website)
            and _official_website_looks_plausible(
                attempt_official_website.strip(),
                working_company_name,
                country,
            )
        ):
            candidate_urls.append(attempt_official_website.strip())
            if not official_website:
                official_website = attempt_official_website.strip()
                summary = "Official website identified from SerpWow search results."
                _log_row_stage(
                    "lookup.serpwow_success",
                    f"attempt={attempt_label} official={_short_text(official_website, 180)!r}",
                    upload_id=debug_upload_id,
                    row_index=debug_row_index,
                )
        for candidate in (attempt_result.get("candidates") or []):
            if (
                isinstance(candidate, str)
                and candidate.strip()
                and not is_disallowed_official_url(candidate)
                and _official_website_looks_plausible(candidate.strip(), working_company_name, country)
            ):
                candidate_urls.append(candidate.strip())
        search_context = {
            "provider": "serpwow",
            "used": bool(attempt_result.get("used")),
            "error": attempt_result.get("error"),
            "raw": attempt_result.get("raw_response"),
        }
        search_attempts.append(
            {
                "attempt": attempt_label,
                "query": query,
                "search_url": attempt_result.get("search_url"),
                "status": "official_website_found" if attempt_official_website else "no_valid_website",
                "used_proxy": False,
                "status_code": attempt_result.get("status_code"),
                "error": attempt_result.get("error"),
                "official_website": attempt_official_website,
            }
        )

        if (
            not allow_phase5_expansion
            or candidate_verification_added
            or len(attempt_queries) >= max_search_attempts
        ):
            return

        raw_attempt = attempt_result.get("raw_response") if isinstance(attempt_result.get("raw_response"), dict) else {}
        phase5_people, phase5_trade_names = _extract_phase5_pivots_from_serpwow(raw_attempt)
        added_count = 0

        for person_name in phase5_people:
            if len(attempt_queries) >= max_search_attempts:
                break
            q_person = f'"{person_name}" "{working_company_name}"'
            normalized_q = re.sub(r"\s+", " ", q_person).strip().lower()
            if normalized_q and normalized_q not in seen_attempt_query_keys:
                seen_attempt_query_keys.add(normalized_q)
                query_item = ("phase5_person_connection", q_person)
                attempt_queries.append(query_item)
                phase5_attempt_queries.append(query_item)
                added_count += 1

        for trade_name in phase5_trade_names:
            if len(attempt_queries) >= max_search_attempts:
                break
            if phase5_plot_marker and phase5_road_marker:
                q_trade_address = f'"{trade_name}" "{phase5_plot_marker}" "{phase5_road_marker}"'
                normalized_q = re.sub(r"\s+", " ", q_trade_address).strip().lower()
                if normalized_q and normalized_q not in seen_attempt_query_keys:
                    seen_attempt_query_keys.add(normalized_q)
                    query_item = ("phase5_trade_address_connection", q_trade_address)
                    attempt_queries.append(query_item)
                    phase5_attempt_queries.append(query_item)
                    added_count += 1
            if len(attempt_queries) >= max_search_attempts:
                break
            q_trade_official = f'"{trade_name}" official website {country}'
            normalized_q = re.sub(r"\s+", " ", q_trade_official).strip().lower()
            if normalized_q and normalized_q not in seen_attempt_query_keys:
                seen_attempt_query_keys.add(normalized_q)
                query_item = ("phase5_trade_official_website", q_trade_official)
                attempt_queries.append(query_item)
                phase5_attempt_queries.append(query_item)
                added_count += 1

        if added_count <= 0 and attempt_candidates:
            best_candidate = ""
            for candidate in attempt_candidates:
                if isinstance(candidate, str) and candidate.strip() and not is_disallowed_official_url(candidate):
                    best_candidate = candidate.strip()
                    break
            if best_candidate:
                parsed_best = urlparse(best_candidate)
                best_domain = (parsed_best.netloc or "").lower()
                if best_domain.startswith("www."):
                    best_domain = best_domain[4:]
                if (
                    best_domain
                    and _candidate_domain_is_plausible_for_company(best_domain, working_company_name, country)
                ):
                    verification_query = (
                        f'"{best_domain}" "{working_company_name}" "{country}"'
                    )
                    normalized_verification = re.sub(r"\s+", " ", verification_query).strip().lower()
                    if normalized_verification and normalized_verification not in seen_attempt_query_keys:
                        seen_attempt_query_keys.add(normalized_verification)
                        query_item = ("phase5_candidate_verification", verification_query)
                        attempt_queries.append(query_item)
                        phase5_attempt_queries.append(query_item)
                        added_count += 1

        if added_count > 0:
            candidate_verification_added = True
            _log_row_stage(
                "lookup.search_plan_expand",
                f"added_phase5_queries={added_count}",
                upload_id=debug_upload_id,
                row_index=debug_row_index,
            )

    timeout_sec = _get_float_env("SERPWOW_TIMEOUT_SEC", 45.0)
    async with httpx.AsyncClient(timeout=timeout_sec) as serpwow_client:
        initial_attempt_queries = list(attempt_queries)
        initial_results = await asyncio.gather(
            *[
                run_serpwow_search(query, country=country, client=serpwow_client)
                for _, query in initial_attempt_queries
            ],
            return_exceptions=True,
        ) if initial_attempt_queries else []

        for (attempt_label, query), raw_result in zip(initial_attempt_queries, initial_results):
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
                }
            _process_serpwow_attempt_result(
                attempt_label=attempt_label,
                query=query,
                attempt_result=raw_result,
                allow_phase5_expansion=True,
            )

        if phase5_attempt_queries:
            phase5_results = await asyncio.gather(
                *[
                    run_serpwow_search(query, country=country, client=serpwow_client)
                    for _, query in phase5_attempt_queries
                ],
                return_exceptions=True,
            )
            for (attempt_label, query), raw_result in zip(phase5_attempt_queries, phase5_results):
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
                    }
                _process_serpwow_attempt_result(
                    attempt_label=attempt_label,
                    query=query,
                    attempt_result=raw_result,
                    allow_phase5_expansion=False,
                )

    enable_gmaps_enrichment = _get_bool_env("ENABLE_GMAPS_FALLBACK", True)
    if enable_gmaps_enrichment:
        gmaps_context = await run_gmaps_from_module(
            company_name=working_company_name,
            country=country,
            input_industry=input_industry,
            input_full_address=working_full_address,
        )
        _log_row_stage(
            "lookup.gmaps",
            (
                f"used={gmaps_context.get('used')} "
                f"request_count={gmaps_context.get('request_count')} "
                f"official={_short_text(gmaps_context.get('official_website'), 140)!r} "
                f"error={_short_text(gmaps_context.get('error'), 200)!r}"
            ),
            upload_id=debug_upload_id,
            row_index=debug_row_index,
        )
        gmaps_website = gmaps_context.get("official_website")
        if (
            isinstance(gmaps_website, str)
            and gmaps_website.strip()
            and _official_website_looks_plausible(gmaps_website.strip(), working_company_name, country)
        ):
            gmaps_website = gmaps_website.strip()
            candidate_urls.append(gmaps_website)
            serp_domain = _normalized_domain(official_website or "")
            gmaps_domain = _normalized_domain(gmaps_website)
            prefer_gmaps = _get_bool_env("PREFER_GMAPS_WEBSITE", True)
            should_replace_with_gmaps = not bool(official_website)
            if (
                not should_replace_with_gmaps
                and prefer_gmaps
                and gmaps_domain
                and gmaps_domain != serp_domain
            ):
                should_replace_with_gmaps = not _candidate_domain_is_plausible_for_company(
                    serp_domain,
                    working_company_name,
                    country,
                )
            if should_replace_with_gmaps:
                official_website = gmaps_website
                summary = "Official website identified from Google Maps details."

    if official_website and not is_disallowed_official_url(official_website):
        candidate_urls.append(official_website)
    dedup_candidates: list[str] = []
    seen_candidates: set[str] = set()
    for candidate in candidate_urls:
        if candidate and candidate not in seen_candidates and not is_disallowed_official_url(candidate):
            seen_candidates.add(candidate)
            dedup_candidates.append(candidate)
    candidate_urls = dedup_candidates
    _log_row_stage(
        "lookup.candidates",
        f"candidate_count={len(candidate_urls)}",
        upload_id=debug_upload_id,
        row_index=debug_row_index,
    )

    enable_final_url_gemini = _get_bool_env("ENABLE_FINAL_URL_GEMINI", True)
    if batch_postprocess_for_upload and enable_final_url_gemini:
        enable_final_url_gemini = False
        final_url_selection_ai_context = {
            "provider": "google-gemini",
            "model": None,
            "used": False,
            "error": "Skipped because Gemini batch post-processing is enabled for upload flow.",
            "usage": {},
            "raw": None,
        }
    if enable_final_url_gemini and candidate_urls:
        final_output, final_error, final_model, final_usage = await asyncio.to_thread(
            choose_final_website_with_gemini,
            working_company_name,
            country,
            input_industry,
            working_full_address,
            candidate_urls,
            search_attempts,
            search_context.get("raw") if isinstance(search_context.get("raw"), dict) else None,
            gmaps_context,
        )
        final_url_selection_ai_context = {
            "provider": "google-gemini",
            "model": final_model,
            "used": final_output is not None,
            "error": final_error,
            "usage": final_usage or {},
            "raw": final_output,
        }
        ai_website = (
            final_output.get("official_website")
            if isinstance(final_output, dict)
            else None
        )
        if (
            isinstance(ai_website, str)
            and ai_website.strip()
            and not is_disallowed_official_url(ai_website)
            and _official_website_looks_plausible(ai_website.strip(), working_company_name, country)
        ):
            official_website = ai_website.strip()
            confidence = str(final_output.get("confidence") or "").strip().lower()
            score = final_output.get("confidence_score")
            summary = f"Official website selected by Gemini confidence scoring ({confidence}, score={score})."
        _log_row_stage(
            "lookup.final_url_ai",
            (
                f"used={final_url_selection_ai_context.get('used')} "
                f"model={_short_text(final_model, 80)!r} "
                f"error={_short_text(final_error, 180)!r} "
                f"selected={_short_text(official_website, 160)!r}"
            ),
            upload_id=debug_upload_id,
            row_index=debug_row_index,
        )

    final_url_ai_cost_usd = calculate_gemini_cost_usd(
        final_url_selection_ai_context.get("usage")
        if isinstance(final_url_selection_ai_context, dict)
        else None
    )
    parsed_city_state_ai_cost_usd = calculate_gemini_cost_usd(
        parsed_city_state_ai_context.get("usage")
        if isinstance(parsed_city_state_ai_context, dict)
        else None
    )
    transliteration_ai_cost_usd = calculate_gemini_cost_usd(
        transliteration_ai_context.get("usage")
        if isinstance(transliteration_ai_context, dict)
        else None
    )
    address_classification_ai_cost_usd = calculate_gemini_cost_usd(
        address_classification_ai_context.get("usage")
        if isinstance(address_classification_ai_context, dict)
        else None
    )

    if not official_website:
        raw_for_s3 = gmaps_context.get("raw_response") or search_context.get("raw")
        serpwow_raw_json = json.dumps(raw_for_s3, ensure_ascii=True, indent=2) if raw_for_s3 is not None else ""
        gmaps_requests_used = int(gmaps_context.get("request_count", 0) or 0)
        total_serpwow_requests = serpwow_search_requests_used + gmaps_requests_used
        total_serpwow_cost_usd = calculate_serpwow_cost_usd(total_serpwow_requests)
        total_gemini_cost_usd = round(
            final_url_ai_cost_usd
            + parsed_city_state_ai_cost_usd
            + transliteration_ai_cost_usd
            + address_classification_ai_cost_usd,
            8,
        )
        total_cost_usd = round(total_serpwow_cost_usd + total_gemini_cost_usd, 8)
        response = CrawlResponse(
            company_name=original_company_name,
            country=country,
            firm_id=firm_id,
            input_industry=input_industry,
            input_full_address=original_input_full_address,
            official_website=None,
            summary=summary,
            address=None,
            phone=None,
            email=None,
            industry=None,
            products=[],
            services=[],
            massive_proxy_cost_usd=0.0,
            serpwow_cost_usd=total_serpwow_cost_usd,
            gemini_cost_usd=total_gemini_cost_usd,
            total_cost_usd=total_cost_usd,
            context={
                "search_url": selected_search_url,
                "search_attempts": search_attempts,
                "status_code": last_status_code,
                "success": False,
                "used_proxy": False,
                "blocked": False,
                "error": search_context.get("error"),
                "ai": search_context,
                "serpwow_mapping_ai": {
                    "provider": "google-gemini",
                    "model": None,
                    "used": False,
                    "error": "Skipped because official_website was not determined.",
                    "raw": None,
                },
                "gmaps": gmaps_context,
                "transliteration_ai": transliteration_ai_context,
                "address_classification_ai": address_classification_ai_context,
                "transliterated_inputs": {
                    "company_name": working_company_name,
                    "full_address": working_full_address,
                },
                "parsed_city_state_ai": parsed_city_state_ai_context,
                "final_url_selection_ai": final_url_selection_ai_context,
                "cost_breakdown": {
                    "massive_proxy_cost_usd": 0.0,
                    "serpwow_cost_usd": total_serpwow_cost_usd,
                    "gemini_cost_usd": total_gemini_cost_usd,
                    "transliteration_ai_cost_usd": transliteration_ai_cost_usd,
                    "address_classification_ai_cost_usd": address_classification_ai_cost_usd,
                    "parsed_city_state_ai_cost_usd": parsed_city_state_ai_cost_usd,
                    "final_url_selection_ai_cost_usd": final_url_ai_cost_usd,
                    "total_cost_usd": total_cost_usd,
                    "serpwow_request_count": total_serpwow_requests,
                },
            },
        )
        elapsed_sec = asyncio.get_event_loop().time() - started_monotonic
        _log_row_stage(
            "lookup.final",
            (
                "result=failed_no_official_website "
                f"search_attempts={len(search_attempts)} "
                f"serpwow_requests={total_serpwow_requests} "
                f"total_cost_usd={total_cost_usd} "
                f"elapsed_sec={elapsed_sec:.2f}"
            ),
            upload_id=debug_upload_id,
            row_index=debug_row_index,
            level="WARN",
        )
        return response, serpwow_raw_json

    if not include_firmographics:
        raw_for_s3 = gmaps_context.get("raw_response") or search_context.get("raw")
        serpwow_raw_json = json.dumps(raw_for_s3, ensure_ascii=True, indent=2) if raw_for_s3 is not None else ""
        gmaps_requests_used = int(gmaps_context.get("request_count", 0) or 0)
        total_serpwow_requests = serpwow_search_requests_used + gmaps_requests_used
        total_serpwow_cost_usd = calculate_serpwow_cost_usd(total_serpwow_requests)
        total_gemini_cost_usd = round(
            final_url_ai_cost_usd
            + parsed_city_state_ai_cost_usd
            + transliteration_ai_cost_usd
            + address_classification_ai_cost_usd,
            8,
        )
        total_cost_usd = round(total_serpwow_cost_usd + total_gemini_cost_usd, 8)
        response = CrawlResponse(
            company_name=original_company_name,
            country=country,
            firm_id=firm_id,
            input_industry=input_industry,
            input_full_address=original_input_full_address,
            official_website=official_website,
            summary="Official website discovered. Firmographic extraction was skipped for URL discovery mode.",
            address=None,
            phone=None,
            email=None,
            industry=None,
            products=[],
            services=[],
            massive_proxy_cost_usd=0.0,
            serpwow_cost_usd=total_serpwow_cost_usd,
            gemini_cost_usd=total_gemini_cost_usd,
            total_cost_usd=total_cost_usd,
            context={
                "pipeline": PIPELINE_URL_DISCOVERY,
                "search_url": selected_search_url,
                "search_attempts": search_attempts,
                "status_code": last_status_code,
                "success": True,
                "used_proxy": False,
                "blocked": False,
                "error": search_context.get("error"),
                "ai": search_context,
                "gmaps": gmaps_context,
                "transliteration_ai": transliteration_ai_context,
                "address_classification_ai": address_classification_ai_context,
                "transliterated_inputs": {
                    "company_name": working_company_name,
                    "full_address": working_full_address,
                },
                "parsed_city_state_ai": parsed_city_state_ai_context,
                "final_url_selection_ai": final_url_selection_ai_context,
                "serpwow_mapping_ai": {
                    "provider": "google-gemini",
                    "model": None,
                    "used": False,
                    "error": "Skipped in URL discovery mode.",
                    "raw": None,
                },
                "cost_breakdown": {
                    "massive_proxy_cost_usd": 0.0,
                    "serpwow_cost_usd": total_serpwow_cost_usd,
                    "gemini_cost_usd": total_gemini_cost_usd,
                    "transliteration_ai_cost_usd": transliteration_ai_cost_usd,
                    "address_classification_ai_cost_usd": address_classification_ai_cost_usd,
                    "parsed_city_state_ai_cost_usd": parsed_city_state_ai_cost_usd,
                    "final_url_selection_ai_cost_usd": final_url_ai_cost_usd,
                    "total_cost_usd": total_cost_usd,
                    "serpwow_request_count": total_serpwow_requests,
                },
            },
        )
        return response, serpwow_raw_json

    serpwow_context: dict[str, Any] = {
        "provider": "serpwow",
        "used": False,
        "domain": None,
        "query": None,
        "ai_overview": None,
        "raw_response": None,
        "error": "Skipped because official_website was not determined.",
    }
    if official_website:
        serpwow_context = await run_serpwow_from_codetails(official_website, country=country)

    mapped_columns = {
        "address": None,
        "phone": None,
        "email": None,
        "industry": None,
        "products": [],
        "services": [],
    }
    serpwow_mapping_ai_context = {
        "provider": "google-gemini",
        "model": None,
        "used": False,
        "error": "Skipped because SerpWow ai_overview was unavailable.",
        "usage": {},
        "raw": None,
    }
    if (
        not batch_postprocess_for_upload
        and official_website
        and isinstance(serpwow_context.get("ai_overview"), dict)
    ):
        mapped_output, mapped_error, mapped_model, mapped_usage = (
            await asyncio.to_thread(
                standardize_serpwow_ai_overview_with_gemini,
                working_company_name,
                country,
                official_website,
                serpwow_context.get("ai_overview") or {},
            )
        )
        if isinstance(mapped_output, dict):
            mapped_columns = {
                "address": mapped_output.get("address"),
                "phone": mapped_output.get("phone"),
                "email": mapped_output.get("email"),
                "industry": mapped_output.get("industry"),
                "products": mapped_output.get("products") or [],
                "services": mapped_output.get("services") or [],
            }
        serpwow_mapping_ai_context = {
            "provider": "google-gemini",
            "model": mapped_model,
            "used": mapped_output is not None,
            "error": mapped_error,
            "usage": mapped_usage or {},
            "raw": mapped_output,
        }
    elif batch_postprocess_for_upload:
        serpwow_mapping_ai_context = {
            "provider": "google-gemini",
            "model": None,
            "used": False,
            "error": "Skipped because Gemini batch post-processing is enabled for upload flow.",
            "usage": {},
            "raw": None,
        }

    if official_website and working_full_address and not batch_postprocess_for_upload:
        mapped_address_value = mapped_columns.get("address")
        if mapped_address_value and not _is_address_aligned(working_full_address, mapped_address_value):
            _log_row_stage(
                "lookup.address_alignment",
                (
                    "aligned=False "
                    f"input_markers={_short_text(', '.join(_address_evidence_markers(working_full_address)), 180)!r} "
                    f"mapped_address={_short_text(mapped_address_value, 180)!r}"
                ),
                upload_id=debug_upload_id,
                row_index=debug_row_index,
                level="WARN",
            )
            summary = (
                "No reliable official website found with address-aligned evidence; "
                "best candidate websites conflict with the provided company address."
            )
            official_website = None
            mapped_columns = {
                "address": None,
                "phone": None,
                "email": None,
                "industry": None,
                "products": [],
                "services": [],
            }
        elif not mapped_address_value:
            _log_row_stage(
                "lookup.address_alignment",
                "aligned=skipped_missing_mapped_address",
                upload_id=debug_upload_id,
                row_index=debug_row_index,
            )
    _log_row_stage(
        "lookup.serpwow_enrichment",
        (
            f"used={serpwow_context.get('used')} "
            f"request_count={serpwow_context.get('request_count')} "
            f"mapping_used={serpwow_mapping_ai_context.get('used')} "
            f"mapping_error={_short_text(serpwow_mapping_ai_context.get('error'), 180)!r}"
        ),
        upload_id=debug_upload_id,
        row_index=debug_row_index,
    )

    serpwow_mapping_ai_cost_usd = calculate_gemini_cost_usd(
        serpwow_mapping_ai_context.get("usage")
        if isinstance(serpwow_mapping_ai_context, dict)
        else None
    )
    serpwow_codetails_requests = int(serpwow_context.get("request_count", 0) or 0)
    gmaps_requests_used = int(gmaps_context.get("request_count", 0) or 0)
    serpwow_total_requests = (
        serpwow_search_requests_used + serpwow_codetails_requests + gmaps_requests_used
    )
    serpwow_cost_usd = calculate_serpwow_cost_usd(serpwow_total_requests)
    total_gemini_cost_usd = round(
        serpwow_mapping_ai_cost_usd
        + final_url_ai_cost_usd
        + parsed_city_state_ai_cost_usd
        + transliteration_ai_cost_usd
        + address_classification_ai_cost_usd,
        8,
    )
    total_cost_usd = round(serpwow_cost_usd + total_gemini_cost_usd, 8)

    response = CrawlResponse(
        company_name=original_company_name,
        country=country,
        firm_id=firm_id,
        input_industry=input_industry,
        input_full_address=original_input_full_address,
        official_website=official_website,
        summary=summary,
        address=mapped_columns.get("address"),
        phone=mapped_columns.get("phone"),
        email=mapped_columns.get("email"),
        industry=mapped_columns.get("industry"),
        products=mapped_columns.get("products") or [],
        services=mapped_columns.get("services") or [],
        massive_proxy_cost_usd=0.0,
        serpwow_cost_usd=serpwow_cost_usd,
        gemini_cost_usd=total_gemini_cost_usd,
        total_cost_usd=total_cost_usd,
        context={
            "search_url": selected_search_url,
            "search_attempts": search_attempts,
            "status_code": last_status_code,
            "success": bool(official_website),
            "used_proxy": False,
            "ai": search_context,
            "serpwow": serpwow_context,
            "serpwow_mapping_ai": serpwow_mapping_ai_context,
            "gmaps": gmaps_context,
            "transliteration_ai": transliteration_ai_context,
            "address_classification_ai": address_classification_ai_context,
            "transliterated_inputs": {
                "company_name": working_company_name,
                "full_address": working_full_address,
            },
            "parsed_city_state_ai": parsed_city_state_ai_context,
            "final_url_selection_ai": final_url_selection_ai_context,
            "cost_breakdown": {
                "massive_proxy_cost_usd": 0.0,
                "serpwow_cost_usd": serpwow_cost_usd,
                "gemini_cost_usd": total_gemini_cost_usd,
                "transliteration_ai_cost_usd": transliteration_ai_cost_usd,
                "address_classification_ai_cost_usd": address_classification_ai_cost_usd,
                "parsed_city_state_ai_cost_usd": parsed_city_state_ai_cost_usd,
                "final_url_selection_ai_cost_usd": final_url_ai_cost_usd,
                "total_cost_usd": total_cost_usd,
                "serpwow_request_count": serpwow_total_requests,
            },
        },
    )
    raw_for_s3 = gmaps_context.get("raw_response") or search_context.get("raw")
    serpwow_raw_json = json.dumps(raw_for_s3, ensure_ascii=True, indent=2) if raw_for_s3 is not None else ""
    elapsed_sec = asyncio.get_event_loop().time() - started_monotonic
    _log_row_stage(
        "lookup.final",
        (
            "result=success "
            f"official={_short_text(official_website, 180)!r} "
            f"search_attempts={len(search_attempts)} "
            f"serpwow_requests={serpwow_total_requests} "
            f"total_cost_usd={total_cost_usd} "
            f"elapsed_sec={elapsed_sec:.2f}"
        ),
        upload_id=debug_upload_id,
        row_index=debug_row_index,
    )
    return response, serpwow_raw_json


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
                retries={"max_attempts": max(1, s3_retries), "mode": "standard"},
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


def _list_state_keys_from_s3_sync(limit: int) -> list[str]:
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        return []

    client = get_s3_client()
    prefix = f"{S3_PREFIX}/"
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


async def write_upload_artifact(upload_id: str, name: str, data: dict[str, Any]) -> None:
    # 1. Always write to the local filesystem first for immediate local consistency
    local_path = _state_file(upload_id) if name == "state" else _output_file(upload_id)
    _write_json(local_path, data)

    # 2. If S3 is enabled, schedule S3 write in the background
    use_s3 = bool(os.getenv("S3_BUCKET"))
    if use_s3:
        company_name = str(data.get("company_name") or "")
        key = _state_s3_key(upload_id, company_name) if name == "state" else _output_s3_key(upload_id, company_name)

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
                return data
        except Exception:
            pass

    # 2. If not found locally and S3 is enabled, download from S3
    use_s3 = bool(os.getenv("S3_BUCKET"))
    if use_s3:
        key = _state_s3_key(upload_id) if name == "state" else _output_s3_key(upload_id)
        data = await asyncio.to_thread(_read_json_from_s3_sync, key)
        # Cache it locally so subsequent reads are instant
        try:
            _write_json(local_path, data)
        except Exception:
            pass
        return data

    raise FileNotFoundError(str(local_path))


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


async def write_upload_text_artifact(upload_id: str, name: str, text: str, content_type: str, company_name: str = "") -> None:
    use_s3 = bool(os.getenv("S3_BUCKET"))
    if use_s3:
        if name == "batch_input_jsonl":
            key = _batch_input_jsonl_s3_key(upload_id, company_name)
        elif name == "batch_output_json":
            key = _batch_output_json_s3_key(upload_id, company_name)
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


def _upload_serpwow_json_sync(upload_id: str, row_index: int, company_name: str, raw_json: str) -> str:
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET not configured")

    safe_name = _safe_name(company_name)
    key = f"{S3_PREFIX}/{upload_id}/{row_index:05d}_{safe_name}_serpwow.json"
    get_s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=raw_json.encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    return key


async def upload_serpwow_json_to_s3(upload_id: str, row_index: int, company_name: str, raw_json: str) -> tuple[Optional[str], Optional[str]]:
    if not raw_json:
        return None, "No SerpWow raw JSON available to upload"
    try:
        key = await asyncio.to_thread(
            _upload_serpwow_json_sync,
            upload_id,
            row_index,
            company_name,
            raw_json,
        )
        return key, None
    except Exception as exc:
        return None, str(exc)


def build_upload_output_payload(state: dict[str, Any]) -> dict[str, Any]:
    timing_summary = build_processing_timing_summary(state.get("rows", []))
    return {
        "upload_id": state["upload_id"],
        "pipeline": state.get("pipeline") or PIPELINE_FULL,
        "status": state["status"],
        "gemini_batch": state.get("gemini_batch"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "total_rows": state["total_rows"],
        "processed_rows": state["processed_rows"],
        "success_rows": state["success_rows"],
        "failed_rows": state["failed_rows"],
        "processing_seconds_total": timing_summary["processing_seconds_total"],
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
                "s3_html_key": row.get("s3_html_key"),
                "s3_serpwow_json_key": row.get("s3_serpwow_json_key"),
                "output": row.get("result"),
            }
            for row in state.get("rows", [])
        ],
    }


def _sanitize_excel_text(value: Any) -> str:
    text = str(value or "")
    # Remove XML-disallowed control chars (keep tab/newline/carriage return).
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)
    # Excel cell text limit.
    return text[:32767]


def _excel_col_name(idx: int) -> str:
    name = ""
    n = idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        name = chr(65 + rem) + name
    return name


def _xlsx_cell_xml(col_idx: int, row_idx: int, value: Any) -> str:
    ref = f"{_excel_col_name(col_idx)}{row_idx}"
    if value is None or value == "":
        return f'<c r="{ref}" t="inlineStr"><is><t></t></is></c>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="n"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}" t="n"><v>{value}</v></c>'
    text = _sanitize_excel_text(value)
    escaped = xml_escape(text)
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escaped}</t></is></c>'


def build_upload_output_xlsx_bytes(output_data: dict[str, Any]) -> bytes:
    headers = [
        "upload_id",
        "file_status",
        "batch_status",
        "created_at",
        "updated_at",
        "total_rows",
        "processed_rows",
        "success_rows",
        "failed_rows",
        "row_index",
        "input_company_name",
        "input_country",
        "input_firm_id",
        "input_industry",
        "input_full_address",
        "input_official_website",
        "row_status",
        "row_error",
        "s3_html_key",
        "s3_serpwow_json_key",
        "output_official_website",
        "output_confidence_score",
        "output_confidence",
        "output_summary",
        "output_website_company_descirption_ai",
        "output_website_company_descirption_translated_ai",
        "output_address",
        "output_phone",
        "output_email",
        "output_industry",
        "output_products",
        "output_services",
        "output_massive_proxy_cost_usd",
        "output_serpwow_cost_usd",
        "output_gemini_cost_usd",
        "output_total_cost_usd",
        "output_processing_seconds",
        "output_json",
    ]

    upload_id = output_data.get("upload_id")
    file_status = output_data.get("status")
    batch_obj = output_data.get("gemini_batch") if isinstance(output_data.get("gemini_batch"), dict) else {}
    batch_status = batch_obj.get("status")
    created_at = output_data.get("created_at")
    updated_at = output_data.get("updated_at")
    total_rows = output_data.get("total_rows")
    processed_rows = output_data.get("processed_rows")
    success_rows = output_data.get("success_rows")
    failed_rows = output_data.get("failed_rows")

    data_rows: list[list[Any]] = []
    for item in output_data.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        input_obj = item.get("input") if isinstance(item.get("input"), dict) else {}
        result_obj = item.get("output") if isinstance(item.get("output"), dict) else {}
        context_obj = result_obj.get("context") if isinstance(result_obj.get("context"), dict) else {}
        final_url_ai_obj = (
            context_obj.get("final_url_selection_ai")
            if isinstance(context_obj.get("final_url_selection_ai"), dict)
            else {}
        )
        final_url_ai_raw = (
            final_url_ai_obj.get("raw")
            if isinstance(final_url_ai_obj.get("raw"), dict)
            else {}
        )
        gemini_batch_ai_obj = (
            context_obj.get("gemini_batch_ai")
            if isinstance(context_obj.get("gemini_batch_ai"), dict)
            else {}
        )
        gemini_batch_ai_raw = (
            gemini_batch_ai_obj.get("raw")
            if isinstance(gemini_batch_ai_obj.get("raw"), dict)
            else {}
        )
        confidence_score = final_url_ai_raw.get("confidence_score")
        if confidence_score is None:
            confidence_score = gemini_batch_ai_raw.get("confidence_score")
        confidence = final_url_ai_raw.get("confidence")
        if confidence is None:
            confidence = gemini_batch_ai_raw.get("confidence")
        data_rows.append(
            [
                upload_id,
                file_status,
                batch_status,
                created_at,
                updated_at,
                total_rows,
                processed_rows,
                success_rows,
                failed_rows,
                item.get("row_index"),
                input_obj.get("company_name"),
                input_obj.get("country"),
                input_obj.get("firm_id"),
                input_obj.get("industry"),
                input_obj.get("full_address"),
                input_obj.get("official_website"),
                item.get("status"),
                item.get("error"),
                item.get("s3_html_key"),
                item.get("s3_serpwow_json_key"),
                result_obj.get("official_website"),
                confidence_score,
                confidence,
                result_obj.get("summary"),
                result_obj.get("website_company_descirption_ai"),
                result_obj.get("website_company_descirption_translated_ai"),
                result_obj.get("address"),
                result_obj.get("phone"),
                result_obj.get("email"),
                result_obj.get("industry"),
                ", ".join(result_obj.get("products") or []) if isinstance(result_obj.get("products"), list) else None,
                ", ".join(result_obj.get("services") or []) if isinstance(result_obj.get("services"), list) else None,
                result_obj.get("massive_proxy_cost_usd"),
                result_obj.get("serpwow_cost_usd"),
                result_obj.get("gemini_cost_usd"),
                result_obj.get("total_cost_usd"),
                (context_obj.get("timing") or {}).get("total_seconds"),
                json.dumps(result_obj, ensure_ascii=True),
            ]
        )

    rows = [headers] + data_rows
    sheet_rows_xml: list[str] = []
    for row_idx, row_values in enumerate(rows, start=1):
        cells_xml = "".join(_xlsx_cell_xml(col_idx, row_idx, value) for col_idx, value in enumerate(row_values, start=1))
        sheet_rows_xml.append(f'<row r="{row_idx}">{cells_xml}</row>')
    sheet_data_xml = "".join(sheet_rows_xml)
    max_col = _excel_col_name(len(headers))
    max_row = max(1, len(rows))
    dimension = f"A1:{max_col}{max_row}"

    worksheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="{dimension}"/>'
        "<sheetViews><sheetView workbookViewId=\"0\"/></sheetViews>"
        "<sheetFormatPr defaultRowHeight=\"15\"/>"
        f"<sheetData>{sheet_data_xml}</sheetData>"
        "</worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        '<sheet name="results" sheetId="1" r:id="rId1"/>'
        "</sheets>"
        "</workbook>"
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )
    root_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", root_rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", worksheet_xml)
        zf.writestr("xl/styles.xml", styles_xml)
    return buffer.getvalue()


def _build_batch_prompt_for_row(row: dict[str, Any]) -> str:
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


def _build_batch_requests_for_state(state: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    requests_payload: list[dict[str, Any]] = []
    row_refs: list[dict[str, Any]] = []
    jsonl_lines: list[str] = []
    for row in state.get("rows", []):
        if not isinstance(row, dict):
            continue
        if row.get("status") not in {"completed", "failed"}:
            continue
        prompt = _build_batch_prompt_for_row(row)
        row_index = int(row.get("row_index", 0) or 0)
        key = f"row-{row_index}"
        request_obj = {
            "request": {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "responseMimeType": "application/json",
                },
            },
            "metadata": {"key": key},
        }
        requests_payload.append(request_obj)
        row_refs.append({"row_index": row_index, "key": key})
        jsonl_lines.append(
            json.dumps(
                {
                    "key": key,
                    "request": request_obj["request"],
                },
                ensure_ascii=True,
            )
        )
    return requests_payload, row_refs, "\n".join(jsonl_lines) + ("\n" if jsonl_lines else "")


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
    if marker not in display_name:
        return None
    tail = display_name.split(marker, 1)[1]
    # Expected: {upload_id}-{uuid}
    candidate = tail.rsplit("-", 1)[0].strip()
    return candidate or None


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
        return "cancel_requested"
    if done_flag:
        return "failed" if error_obj else "succeeded"

    if local in {
        "waiting_for_rows",
        "queued",
        "running",
        "cancel_requested",
        "succeeded",
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


async def run_gemini_batch_for_upload(upload_id: str) -> None:
    started_monotonic = asyncio.get_event_loop().time()
    batch_name = ""
    final_state_name = ""
    try:
        existing_job_name = ""
        started_at_value: Optional[str] = None
        async with get_upload_lock(upload_id):
            state = await read_upload_artifact(upload_id, "state")
            gemini_batch_meta = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
            if gemini_batch_meta.get("status") == "succeeded":
                return
            existing_job_name = str(gemini_batch_meta.get("job_name") or "").strip()
            started_at_value = gemini_batch_meta.get("started_at") or _now_iso()
            state["gemini_batch"] = {
                "status": "running",
                "started_at": started_at_value,
                "job_name": existing_job_name or None,
                "error": None,
            }
            await persist_upload_state(upload_id, state)

        _batch_company_name = str(state.get("company_name") or "")
        requests_payload, row_refs, jsonl_text = _build_batch_requests_for_state(state)
        if not requests_payload:
            async with get_upload_lock(upload_id):
                state = await read_upload_artifact(upload_id, "state")
                state["gemini_batch"] = {
                    "status": "skipped",
                    "started_at": state.get("gemini_batch", {}).get("started_at"),
                    "completed_at": _now_iso(),
                    "job_name": None,
                    "error": "No rows available for Gemini batch processing.",
                }
                await persist_upload_state(upload_id, state)
            return

        batch_model = os.getenv("GEMINI_BATCH_MODEL", os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"))
        batch_name = existing_job_name

        if not batch_name:
            await write_upload_text_artifact(
                upload_id,
                "batch_input_jsonl",
                jsonl_text,
                "application/x-ndjson; charset=utf-8",
                company_name=_batch_company_name,
            )

            create_resp = await asyncio.to_thread(
                _gemini_batch_create_sync,
                batch_model,
                requests_payload,
                upload_id,
            )
            batch_name = str(create_resp.get("name") or "")
            if not batch_name:
                raise RuntimeError(f"Unexpected Gemini batch create response: {create_resp}")
            _log_gemini_batch(
                upload_id,
                f"submitted job_name={batch_name} model={batch_model} rows={len(row_refs)}",
            )
        else:
            _log_gemini_batch(
                upload_id,
                f"resumed existing job_name={batch_name} model={batch_model} rows={len(row_refs)}",
            )

        async with get_upload_lock(upload_id):
            state = await read_upload_artifact(upload_id, "state")
            state["gemini_batch"] = {
                "status": "running",
                "started_at": state.get("gemini_batch", {}).get("started_at"),
                "job_name": batch_name,
                "error": None,
            }
            await persist_upload_state(upload_id, state)

        poll_interval = max(5, _get_int_env("GEMINI_BATCH_POLL_SEC", 15))
        poll_timeout = max(60, _get_int_env("GEMINI_BATCH_TIMEOUT_SEC", 1800))
        deadline = asyncio.get_event_loop().time() + poll_timeout
        _log_gemini_batch(
            upload_id,
            f"polling started job_name={batch_name} poll_interval={poll_interval}s timeout={poll_timeout}s",
        )
        final_batch_obj: Optional[dict[str, Any]] = None
        transient_poll_errors = 0
        last_poll_error: Optional[str] = None
        while True:
            try:
                batch_obj = await asyncio.to_thread(_gemini_batch_get_sync, batch_name)
                transient_poll_errors = 0
                last_poll_error = None
            except Exception as exc:
                transient_poll_errors += 1
                last_poll_error = f"{type(exc).__name__}: {str(exc)}"
                if asyncio.get_event_loop().time() >= deadline:
                    raise TimeoutError(
                        f"Gemini batch status polling timed out after {poll_timeout}s; "
                        f"last_error={last_poll_error}"
                    ) from exc
                _log_gemini_batch(
                    upload_id,
                    (
                        f"poll_transient_error count={transient_poll_errors} "
                        f"job_name={batch_name} error={_short_text(last_poll_error, 220)!r}"
                    ),
                )
                await asyncio.sleep(min(poll_interval, 5))
                continue

            state_name = _gemini_batch_state_name(batch_obj)
            done_flag = bool(batch_obj.get("done"))
            if _gemini_batch_is_terminal(state_name, done_flag):
                final_batch_obj = batch_obj
                break
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(
                    f"Gemini batch timed out after {poll_timeout}s"
                    + (f"; last_poll_error={last_poll_error}" if last_poll_error else "")
                )
            await asyncio.sleep(poll_interval)

        await write_upload_text_artifact(
            upload_id,
            "batch_output_json",
            json.dumps(final_batch_obj, ensure_ascii=True, indent=2),
            "application/json; charset=utf-8",
            company_name=_batch_company_name,
        )

        final_state_name = _gemini_batch_state_name(final_batch_obj if isinstance(final_batch_obj, dict) else {})
        done_flag = bool(final_batch_obj.get("done")) if isinstance(final_batch_obj, dict) else False
        if not _gemini_batch_is_success(final_state_name, done_flag, final_batch_obj if isinstance(final_batch_obj, dict) else {}):
            error_obj = final_batch_obj.get("error") if isinstance(final_batch_obj, dict) else None
            raise RuntimeError(f"Gemini batch ended with state={final_state_name} error={error_obj}")

        inlined = _extract_batch_inlined_responses(final_batch_obj if isinstance(final_batch_obj, dict) else {})

        parsed_by_row: dict[int, dict[str, Any]] = {}
        usage_by_row: dict[int, dict[str, Any]] = {}
        usage_total_prompt = 0
        usage_total_candidates = 0
        row_index_by_key: dict[str, int] = {
            str(ref.get("key") or "").strip(): int(ref.get("row_index", 0) or 0)
            for ref in row_refs
            if isinstance(ref, dict) and str(ref.get("key") or "").strip()
        }
        use_index_fallback = len(inlined) == len(row_refs)
        mapped_by_key_count = 0
        mapped_by_index_count = 0
        unmapped_count = 0
        for idx, inline_item in enumerate(inlined):
            if not isinstance(inline_item, dict):
                continue
            response_key = _extract_batch_response_key(inline_item)
            row_index = row_index_by_key.get(response_key) if response_key else None
            if row_index:
                mapped_by_key_count += 1
            elif use_index_fallback and idx < len(row_refs):
                row_index = row_refs[idx]["row_index"]
                mapped_by_index_count += 1
            if row_index is None:
                unmapped_count += 1
                continue
            response_obj = inline_item.get("response") if isinstance(inline_item.get("response"), dict) else {}
            text = _extract_text_from_generate_response(response_obj)
            parsed = _parse_json_from_text(text) or {}
            usage = response_obj.get("usageMetadata") if isinstance(response_obj.get("usageMetadata"), dict) else {}
            usage_total_prompt += int(usage.get("promptTokenCount", 0) or 0)
            usage_total_candidates += int(usage.get("candidatesTokenCount", 0) or 0)
            parsed_by_row[int(row_index)] = parsed
            usage_by_row[int(row_index)] = usage

        _log_gemini_batch(
            upload_id,
            (
                f"response_mapping inlined={len(inlined)} row_refs={len(row_refs)} "
                f"mapped_by_key={mapped_by_key_count} mapped_by_index={mapped_by_index_count} "
                f"unmapped={unmapped_count} index_fallback={use_index_fallback}"
            ),
        )

        batch_usage = {
            "promptTokenCount": usage_total_prompt,
            "candidatesTokenCount": usage_total_candidates,
        }
        batch_cost_usd = calculate_gemini_batch_cost_usd(batch_usage)

        async with get_upload_lock(upload_id):
            state = await read_upload_artifact(upload_id, "state")
            batch_rows_touched = 0
            batch_rows_completed = 0
            batch_rows_failed = 0
            for row in state.get("rows", []):
                if not isinstance(row, dict):
                    continue
                row_index = int(row.get("row_index", 0) or 0)
                parsed = parsed_by_row.get(row_index)
                if not isinstance(parsed, dict):
                    continue
                batch_rows_touched += 1
                result = row.get("result") if isinstance(row.get("result"), dict) else {}
                context = result.get("context") if isinstance(result.get("context"), dict) else {}
                row_usage = usage_by_row.get(row_index) if isinstance(usage_by_row.get(row_index), dict) else {}
                row_batch_cost_usd = calculate_gemini_batch_cost_usd(row_usage)
                prev_gemini_cost = _as_float(result.get("gemini_cost_usd"), 0.0)
                prev_total_cost = _as_float(result.get("total_cost_usd"), 0.0)
                updated_gemini_cost = round(prev_gemini_cost + row_batch_cost_usd, 8)
                updated_total_cost = round(prev_total_cost + row_batch_cost_usd, 8)
                selected_url = parsed.get("official_website")
                if isinstance(selected_url, str) and selected_url.strip() and not is_disallowed_official_url(selected_url):
                    candidate_url = selected_url.strip()
                    if _official_website_looks_plausible(
                        candidate_url,
                        str(row.get("company_name") or ""),
                        str(row.get("country") or ""),
                    ):
                        result["official_website"] = candidate_url
                result["summary"] = str(parsed.get("summary") or result.get("summary") or "")
                if parsed.get("website_company_descirption_ai") is not None:
                    result["website_company_descirption_ai"] = parsed.get(
                        "website_company_descirption_ai"
                    )
                if parsed.get("website_company_descirption_translated_ai") is not None:
                    result["website_company_descirption_translated_ai"] = parsed.get(
                        "website_company_descirption_translated_ai"
                    )
                if parsed.get("address") is not None:
                    result["address"] = parsed.get("address")
                if parsed.get("phone") is not None:
                    result["phone"] = parsed.get("phone")
                if parsed.get("email") is not None:
                    result["email"] = parsed.get("email")
                if parsed.get("industry") is not None:
                    result["industry"] = parsed.get("industry")
                if isinstance(parsed.get("products"), list):
                    result["products"] = parsed.get("products")
                if isinstance(parsed.get("services"), list):
                    result["services"] = parsed.get("services")
                result["gemini_cost_usd"] = updated_gemini_cost
                result["total_cost_usd"] = updated_total_cost
                cost_breakdown = context.get("cost_breakdown") if isinstance(context.get("cost_breakdown"), dict) else {}
                cost_breakdown["gemini_batch_cost_usd"] = round(
                    _as_float(cost_breakdown.get("gemini_batch_cost_usd"), 0.0) + row_batch_cost_usd,
                    8,
                )
                cost_breakdown["gemini_cost_usd"] = updated_gemini_cost
                cost_breakdown["total_cost_usd"] = updated_total_cost
                context["cost_breakdown"] = cost_breakdown
                context["gemini_batch_ai"] = {
                    "provider": "google-gemini-batch",
                    "model": batch_model,
                    "used": True,
                    "usage": row_usage,
                    "cost_usd": row_batch_cost_usd,
                    "raw": parsed,
                    "error": None,
                }
                result["context"] = context
                row["result"] = result
                finalized_official = str(result.get("official_website") or "").strip()
                is_plausible_official = (
                    bool(finalized_official)
                    and _official_website_looks_plausible(
                        finalized_official,
                        str(row.get("company_name") or ""),
                        str(row.get("country") or ""),
                    )
                )
                if not is_plausible_official:
                    result["official_website"] = None
                row["status"] = "completed" if is_plausible_official else "failed"
                row["error"] = None if row["status"] == "completed" else (
                    "Official website not found after Gemini batch post-processing."
                )
                if row["status"] == "completed":
                    batch_rows_completed += 1
                else:
                    batch_rows_failed += 1
            state["gemini_batch"] = {
                "status": "succeeded",
                "started_at": state.get("gemini_batch", {}).get("started_at"),
                "completed_at": _now_iso(),
                "job_name": batch_name,
                "error": None,
                "usage": batch_usage,
                "batch_cost_usd": batch_cost_usd,
            }
            await persist_upload_state(upload_id, state)
        elapsed_sec = asyncio.get_event_loop().time() - started_monotonic
        _log_gemini_batch(
            upload_id,
            (
                f"completed job_name={batch_name} state={final_state_name} "
                f"duration_sec={elapsed_sec:.2f} rows_with_output={len(parsed_by_row)} "
                f"batch_cost_usd={batch_cost_usd} rows_touched={batch_rows_touched} "
                f"rows_completed={batch_rows_completed} rows_failed={batch_rows_failed}"
            ),
        )
    except Exception as exc:
        elapsed_sec = asyncio.get_event_loop().time() - started_monotonic
        _log_gemini_batch(
            upload_id,
            (
                f"failed job_name={batch_name or 'unknown'} state={final_state_name or 'unknown'} "
                f"duration_sec={elapsed_sec:.2f} error_type={type(exc).__name__} error={repr(exc)}"
            ),
        )
        async with get_upload_lock(upload_id):
            try:
                state = await read_upload_artifact(upload_id, "state")
                state["gemini_batch"] = {
                    "status": "failed",
                    "started_at": (state.get("gemini_batch") or {}).get("started_at"),
                    "completed_at": _now_iso(),
                    "job_name": (state.get("gemini_batch") or {}).get("job_name"),
                    "error": str(exc),
                }
                await persist_upload_state(upload_id, state)
            except Exception:
                pass
    finally:
        gemini_batch_tasks.pop(upload_id, None)


async def maybe_start_gemini_batch_for_upload(upload_id: str, state: dict[str, Any]) -> None:
    if str(state.get("pipeline") or PIPELINE_FULL) != PIPELINE_FULL:
        return
    if not _get_bool_env("ENABLE_GEMINI_BATCH_POSTPROCESS", False):
        return
    if state.get("status") not in {"completed", "completed_with_errors"}:
        return
    gemini_batch_meta = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
    if gemini_batch_meta.get("status") in {"queued", "running", "succeeded"}:
        return
    if upload_id in gemini_batch_tasks:
        return
    state["gemini_batch"] = {
        "status": "queued",
        "queued_at": _now_iso(),
        "job_name": None,
        "error": None,
    }
    await persist_upload_state(upload_id, state)
    gemini_batch_tasks[upload_id] = asyncio.create_task(run_gemini_batch_for_upload(upload_id))


async def maybe_resume_gemini_batch_for_upload(upload_id: str, state: dict[str, Any]) -> None:
    if str(state.get("pipeline") or PIPELINE_FULL) != PIPELINE_FULL:
        return
    if not _get_bool_env("ENABLE_GEMINI_BATCH_POSTPROCESS", False):
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
    if str(state.get("pipeline") or PIPELINE_FULL) != PIPELINE_FULL:
        return state
    gemini_batch_meta = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
    local_status = str(gemini_batch_meta.get("status") or "").strip()
    job_name = str(gemini_batch_meta.get("job_name") or "").strip()
    started_at_dt = _parse_iso_datetime(gemini_batch_meta.get("started_at"))
    startup_timeout_sec = max(30.0, _get_float_env("GEMINI_BATCH_STARTUP_TIMEOUT_SEC", 180.0))

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
            elif resolved_status in {"succeeded", "running"}:
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


def _normalize_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def _validate_canonical_upload_csv(raw: bytes) -> None:
    """Unified CSV validation gate (spec §3) for the SerpWow upload pipelines.

    Runs the canonical validator purely as a gate: garbage files (e.g. legacy
    Company-Mode exports whose header row would otherwise be ingested as data
    by ``parse_csv_rows``) are rejected with a 400 up front. On success the
    callers still use the legacy ``parse_csv_rows`` to build the row dicts the
    SerpWow pipelines expect. NOT applied to /uploads/firmographics, which has
    its own website-column format and parser.
    """
    from app.models.entities import InvalidCSVError, parse_entities_csv

    try:
        parse_entities_csv(raw)
    except InvalidCSVError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def parse_csv_rows(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8-sig", errors="replace")
    stream = io.StringIO(text)
    reader = csv.DictReader(stream)

    rows: list[dict[str, str]] = []
    if reader.fieldnames:
        normalized = {_normalize_header(h): h for h in reader.fieldnames if h}
        company_key = None
        country_key = None
        firm_id_key = None
        industry_key = None
        full_address_key = None
        for key in (
            "company_name",
            "company",
            "name",
            "entity_name",
            "entity",
            "organization",
            "organisation",
            "legal_name",
        ):
            if key in normalized:
                company_key = normalized[key]
                break
        for key in ("country", "country_name", "nation"):
            if key in normalized:
                country_key = normalized[key]
                break
        for key in ("firm_id", "firmid", "id"):
            if key in normalized:
                firm_id_key = normalized[key]
                break
        for key in ("industry", "input_industry"):
            if key in normalized:
                industry_key = normalized[key]
                break
        for key in ("full_address", "address", "fulladdress", "input_full_address"):
            if key in normalized:
                full_address_key = normalized[key]
                break

        if company_key and country_key:
            for idx, row in enumerate(reader, start=1):
                company_name = (row.get(company_key) or "").strip()
                country = (row.get(country_key) or "").strip()
                if not company_name:
                    continue
                rows.append({
                    "row_index": idx,
                    "company_name": company_name,
                    "country": country,
                    "firm_id": (row.get(firm_id_key) or "").strip() if firm_id_key else "",
                    "industry": (row.get(industry_key) or "").strip() if industry_key else "",
                    "full_address": (row.get(full_address_key) or "").strip() if full_address_key else "",
                })

    if rows:
        return rows

    stream.seek(0)
    plain_reader = csv.reader(stream)
    for idx, cols in enumerate(plain_reader, start=1):
        if len(cols) < 2:
            continue
        if idx == 1:
            first_norm = _normalize_header(str(cols[0] or ""))
            second_norm = _normalize_header(str(cols[1] or ""))
            if first_norm in {
                "company_name",
                "company",
                "name",
                "entity_name",
                "entity",
                "organization",
                "organisation",
                "legal_name",
            } and second_norm in {"country", "country_name", "nation"}:
                # Header-like row in plain fallback mode; skip instead of ingesting as data.
                continue
        company_name = (cols[0] or "").strip()
        country = (cols[1] or "").strip()
        if not company_name:
            continue
        rows.append(
            {
                "row_index": idx,
                "company_name": company_name,
                "country": country,
                "firm_id": "",
                "industry": "",
                "full_address": "",
            }
        )

    if not rows:
        raise ValueError("CSV must include company and country columns (or first two columns).")
    return rows


def parse_firmographics_csv_rows(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8-sig", errors="replace")
    stream = io.StringIO(text)
    reader = csv.DictReader(stream)

    rows: list[dict[str, str]] = []
    if not reader.fieldnames:
        raise ValueError(
            "CSV must include headers for firmographics upload. Required: official_website (or website/url/domain)."
        )

    normalized = {_normalize_header(h): h for h in reader.fieldnames if h}
    website_key = None
    company_key = None
    country_key = None
    firm_id_key = None
    industry_key = None
    full_address_key = None

    for key in ("official_website", "website", "url", "domain"):
        if key in normalized:
            website_key = normalized[key]
            break
    for key in ("company_name", "company", "name"):
        if key in normalized:
            company_key = normalized[key]
            break
    for key in ("country", "country_name", "nation"):
        if key in normalized:
            country_key = normalized[key]
            break
    for key in ("firm_id", "firmid", "id"):
        if key in normalized:
            firm_id_key = normalized[key]
            break
    for key in ("industry", "input_industry"):
        if key in normalized:
            industry_key = normalized[key]
            break
    for key in ("full_address", "address", "fulladdress", "input_full_address"):
        if key in normalized:
            full_address_key = normalized[key]
            break

    if not website_key:
        raise ValueError(
            "Firmographics CSV must include official_website (or website/url/domain) column."
        )

    for idx, row in enumerate(reader, start=1):
        official_website = _normalize_website_input(str(row.get(website_key) or ""))
        if not official_website:
            continue
        company_name = (row.get(company_key) or "").strip() if company_key else ""
        country = (row.get(country_key) or "").strip() if country_key else ""
        if not company_name:
            company_name = _domain_from_url(official_website)
        rows.append(
            {
                "row_index": idx,
                "company_name": company_name,
                "country": country,
                "firm_id": (row.get(firm_id_key) or "").strip() if firm_id_key else "",
                "industry": (row.get(industry_key) or "").strip() if industry_key else "",
                "full_address": (row.get(full_address_key) or "").strip() if full_address_key else "",
                "official_website": official_website,
            }
        )

    if not rows:
        raise ValueError("Firmographics CSV has no valid rows with official_website.")
    return rows


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


def summarize_upload_state(state: dict[str, Any]) -> dict[str, Any]:
    if not state.get("pipeline"):
        state["pipeline"] = PIPELINE_FULL
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
    state.update(build_processing_timing_summary(rows))
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
                    "error": row.get("error"),
                    "official_website": official_website or None,
                    "status_updated_at": row.get("status_updated_at"),
                }
            )

    def _sort_counts(counts: dict[str, int], top_n: int = 20) -> list[dict[str, Any]]:
        items = sorted(counts.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))
        return [{"reason": k, "count": v} for k, v in items[:top_n]]

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
        "sample_failed_rows": sample_failed,
    }


def _upload_file_links(upload_id: str, company_name: str = "") -> dict[str, str]:
    bucket = os.getenv("S3_BUCKET")
    if bucket:
        return {
            "state.json": f"s3://{bucket}/{_state_s3_key(upload_id, company_name)}",
            "output.json": f"s3://{bucket}/{_output_s3_key(upload_id, company_name)}",
        }
    return {
        "state.json": str(_find_upload_dir(upload_id) / "state.json"),
        "output.json": str(_find_upload_dir(upload_id) / "output.json"),
    }


def update_summary_cache(upload_id: str, state: dict[str, Any]) -> None:
    try:
        summary = summarize_upload_state(dict(state))
        state_pipeline = str(summary.get("pipeline") or PIPELINE_FULL)
        file_links = _upload_file_links(upload_id, str(summary.get("company_name") or ""))
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
        file_links = _upload_file_links(upload_id, str(state.get("company_name") or ""))
        return svc.update_run(
            run_db_id,
            status=str(state.get("status") or ""),
            success_count=state.get("success_rows"),
            failed_count=state.get("failed_rows"),
            duration_seconds=state.get("processing_seconds_total"),
            file_links=file_links,
            finished_at=_now_iso(),
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


async def persist_upload_state(upload_id: str, state: dict[str, Any]) -> None:
    state = summarize_upload_state(state)
    if _should_sync_running(state):
        # One-shot 'running' sync: the marker is set regardless of outcome so a
        # down Supabase (update_run retries 3x with sleeps) can't re-stall every
        # subsequent persist; the terminal sync below has its own retry story.
        state["supabase_running_marker"] = True
        await asyncio.to_thread(_mark_supabase_run_running, dict(state))
    if state.get("run_db_id") and state.get("status") in {
        "completed",
        "completed_with_errors",
        "failed",
    }:
        # Sync once per distinct terminal snapshot (retries can re-open an upload
        # and re-complete it with different counters; resync then).
        marker = (
            f"{state.get('status')}:{state.get('success_rows')}:{state.get('failed_rows')}"
        )
        if _should_sync_supabase(state, marker):
            if await asyncio.to_thread(_update_supabase_run, dict(state)):
                state["supabase_sync_marker"] = marker
            else:
                # Mark the failure so we don't retry this exact snapshot on
                # every persist (see _should_sync_supabase for the trade-off).
                state["supabase_sync_failed_marker"] = marker
    update_summary_cache(upload_id, state)
    await write_upload_artifact(upload_id, "state", state)

    # Writing output.json on each row update adds significant S3 overhead.
    # Persist it only when upload is complete; otherwise /output builds from state on demand.
    if state["status"] in {"completed", "completed_with_errors"}:
        combined = build_upload_output_payload(state)
        await write_upload_artifact(upload_id, "output", combined)
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
    s3_html_key: Optional[str] = None,
    s3_serpwow_json_key: Optional[str] = None,
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
        if s3_html_key is not None:
            row["s3_html_key"] = s3_html_key
        if s3_serpwow_json_key is not None:
            row["s3_serpwow_json_key"] = s3_serpwow_json_key
        await persist_upload_state(upload_id, state)


async def maybe_requeue_stuck_queued_rows(upload_id: str, state: dict[str, Any]) -> dict[str, Any]:
    if rabbitmq_exchange is None or rabbitmq_queue is None:
        return state
    if str(state.get("pipeline") or PIPELINE_FULL) not in {
        PIPELINE_FULL,
        PIPELINE_URL_DISCOVERY,
        PIPELINE_FIRMOGRAPHICS,
        PIPELINE_GMAPS,
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

    pipeline = str(state.get("pipeline") or PIPELINE_FULL)
    phase = str(state.get("phase") or "all")
    jobs = [_build_row_job_payload(upload_id, row, pipeline, phase) for row in queued_rows]
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


async def close_rabbitmq() -> None:
    global rabbitmq_connection, rabbitmq_channel, rabbitmq_exchange, rabbitmq_queue, rabbitmq_consumer_tasks

    await stop_worker_consumers()

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


async def process_upload_job(job: dict[str, Any]) -> None:
    upload_id = str(job["upload_id"])
    row_index = int(job["row_index"])
    company_name = str(job["company_name"])
    country = str(job["country"])
    firm_id = str(job.get("firm_id") or "") or None
    input_industry = str(job.get("industry") or "") or None
    input_full_address = str(job.get("full_address") or "") or None
    official_website_input = str(job.get("official_website") or "") or None
    pipeline = str(job.get("pipeline") or PIPELINE_FULL).strip() or PIPELINE_FULL
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
        elif pipeline == PIPELINE_GMAPS:
            crawl_response, serpwow_raw_json = await execute_gmaps_lookup(
                company_name=company_name,
                country=country,
                firm_id=firm_id,
                input_industry=input_industry,
                input_full_address=input_full_address,
                debug_upload_id=upload_id,
                debug_row_index=row_index,
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
            crawl_response, serpwow_raw_json = await execute_company_lookup(
                company_name=company_name,
                country=country,
                firm_id=firm_id,
                input_industry=input_industry,
                input_full_address=input_full_address,
                debug_upload_id=upload_id,
                debug_row_index=row_index,
                include_firmographics=(pipeline != PIPELINE_URL_DISCOVERY),
            )

        s3_serpwow_json_key, s3_error = await upload_serpwow_json_to_s3(
            upload_id=upload_id,
            row_index=row_index,
            company_name=company_name,
            raw_json=serpwow_raw_json,
        )

        result = crawl_response.model_dump()
        result["s3_serpwow_json_key"] = s3_serpwow_json_key
        result["s3_serpwow_json_error"] = s3_error
        # Backward compatibility for clients still reading old keys.
        result["s3_html_key"] = s3_serpwow_json_key
        result["s3_html_error"] = s3_error
        # Record total per-row processing time so each pipeline's flow duration is visible
        # in the output/XLSX (parallels the AI-mode per-batch timing).
        if isinstance(result.get("context"), dict):
            result["context"]["timing"] = {
                "total_seconds": round(asyncio.get_event_loop().time() - started_monotonic, 3)
            }

        official_website = (result.get("official_website") or "").strip()
        is_successful = bool(official_website)
        batch_postprocess_enabled = _get_bool_env("ENABLE_GEMINI_BATCH_POSTPROCESS", False)
        if is_successful:
            row_status = "completed"
            row_error = None
        elif batch_postprocess_enabled:
            # Keep rows non-failed while batch post-processing is still expected to decide.
            row_status = "completed"
            row_error = "Pending Gemini batch post-processing decision."
        else:
            row_status = "failed"
            row_error = "Official website not found; target blocked/unreachable or no valid result."

        await update_row_state(
            upload_id,
            row_index,
            status=row_status,
            error=row_error,
            result=result,
            s3_html_key=s3_serpwow_json_key,
            s3_serpwow_json_key=s3_serpwow_json_key,
        )
        elapsed_sec = asyncio.get_event_loop().time() - started_monotonic
        _log_row_stage(
            "worker.row_done",
            (
                f"status={row_status} "
                f"official={_short_text(official_website, 160)!r} "
                f"s3_key={_short_text(s3_serpwow_json_key, 140)!r} "
                f"s3_error={_short_text(s3_error, 180)!r} "
                f"elapsed_sec={elapsed_sec:.2f}"
            ),
            upload_id=upload_id,
            row_index=row_index,
            level="INFO" if is_successful else "WARN",
        )
    except Exception as exc:
        try:
            await update_row_state(upload_id, row_index, status="failed", error=str(exc))
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


async def start_worker_consumers(worker_count: Optional[int] = None) -> None:
    global rabbitmq_consumer_tasks, rabbitmq_stop_event
    if rabbitmq_queue is None:
        raise RuntimeError("RabbitMQ queue is not initialized")
    if rabbitmq_consumer_tasks:
        return

    rabbitmq_stop_event = asyncio.Event()
    count = worker_count if worker_count is not None else _get_int_env("WORKER_CONCURRENCY", 4)
    count = max(1, int(count))
    rabbitmq_consumer_tasks = [
        asyncio.create_task(rabbitmq_worker_loop(i + 1))
        for i in range(count)
    ]


async def stop_worker_consumers() -> None:
    global rabbitmq_consumer_tasks, rabbitmq_stop_event
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
    for task in list(gemini_batch_tasks.values()):
        task.cancel()
    if gemini_batch_tasks:
        await asyncio.gather(*gemini_batch_tasks.values(), return_exceptions=True)
        gemini_batch_tasks.clear()
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


@app.get("/crawl", response_model=CrawlResponse)
async def crawl_url_get(
    company_name: str,
    country: str,
    firm_id: Optional[str] = None,
    industry: Optional[str] = None,
    full_address: Optional[str] = None,
) -> CrawlResponse:
    response, _ = await execute_company_lookup(
        company_name,
        country,
        firm_id=firm_id,
        input_industry=industry,
        input_full_address=full_address,
    )
    return response


@app.post("/crawl", response_model=CrawlResponse)
async def crawl_url_post(payload: CrawlRequest) -> CrawlResponse:
    response, _ = await execute_company_lookup(
        company_name=payload.company_name,
        country=payload.country,
        firm_id=payload.firm_id,
        input_industry=payload.industry,
        input_full_address=payload.full_address,
    )
    return response


@app.post("/crawl/url-discovery", response_model=CrawlResponse)
async def crawl_url_discovery_post(payload: CrawlRequest) -> CrawlResponse:
    response, _ = await execute_company_lookup(
        company_name=payload.company_name,
        country=payload.country,
        firm_id=payload.firm_id,
        input_industry=payload.industry,
        input_full_address=payload.full_address,
        include_firmographics=False,
    )
    return response


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
        total_rows=len(parsed_rows),
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
                "s3_html_key": None,
                "s3_serpwow_json_key": None,
                "result": None,
            }
            for row in parsed_rows
        ],
    }
    if pipeline == PIPELINE_FULL and _get_bool_env("ENABLE_GEMINI_BATCH_POSTPROCESS", False):
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
        "output_xlsx_url": f"/uploads/{upload_id}/output?format=xlsx",
    }


@app.post("/uploads")
async def create_upload(
    file: UploadFile = File(...),
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
        file, parsed_rows, PIPELINE_FULL, company_id=company_id, company_name=company_name
    )


@app.post("/uploads/url-discovery")
async def create_url_discovery_upload(
    file: UploadFile = File(...),
    company_id: str = Form(...),
) -> dict[str, Any]:
    raw = await file.read()
    _validate_canonical_upload_csv(raw)
    try:
        parsed_rows = parse_csv_rows(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _create_upload_with_rows(
        file, parsed_rows, PIPELINE_URL_DISCOVERY, company_id=company_id
    )


@app.post("/uploads/firmographics")
async def create_firmographics_upload(
    file: UploadFile = File(...),
    company_id: str = Form(...),
    company_name: str = Form(""),
) -> dict[str, Any]:
    raw = await file.read()
    try:
        parsed_rows = parse_firmographics_csv_rows(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _create_upload_with_rows(
        file, parsed_rows, PIPELINE_FIRMOGRAPHICS, company_id=company_id, company_name=company_name
    )


@app.post("/uploads/gmaps")
async def create_gmaps_upload(
    file: UploadFile = File(...),
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
        file, parsed_rows, PIPELINE_GMAPS, company_id=company_id, company_name=company_name
    )


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


@app.post("/uploads/{upload_id}/retry-failed-rows")
async def retry_failed_rows(
    upload_id: str,
    limit: int = Query(0, ge=0, le=5000),
) -> dict[str, Any]:
    if rabbitmq_exchange is None:
        detail = "RabbitMQ not connected. Try again shortly."
        if rabbitmq_last_error:
            detail = f"{detail} Last error: {rabbitmq_last_error}"
        raise HTTPException(status_code=503, detail=detail)

    try:
        await get_upload_state(upload_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload ID not found") from exc

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

        pipeline = str(state.get("pipeline") or PIPELINE_FULL)
        phase = str(state.get("phase") or "all")
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
            row["s3_html_key"] = None
            row["s3_serpwow_json_key"] = None
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
                }
            )

        if pipeline == PIPELINE_FULL and _get_bool_env("ENABLE_GEMINI_BATCH_POSTPROCESS", False):
            state["gemini_batch"] = {
                "status": "waiting_for_rows",
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


@app.get("/uploads")
async def uploads_list(
    limit: int = Query(100, ge=1, le=500),
    pipeline: Optional[str] = Query(None),
) -> dict[str, Any]:
    pipeline_value = None
    if pipeline:
        candidate = str(pipeline).strip().lower()
        if candidate in {PIPELINE_FULL, PIPELINE_URL_DISCOVERY, PIPELINE_FIRMOGRAPHICS, PIPELINE_GMAPS, PIPELINE_GSEARCH}:
            pipeline_value = candidate
    items = await list_upload_summaries(limit, pipeline=pipeline_value)
    return {"count": len(items), "uploads": items}


@app.get("/batch/jobs")
async def batch_jobs_list(limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    global gemini_batch_list_cache, gemini_batch_list_cache_fetched_at, gemini_batch_list_error_cooldown_until
    items = await list_upload_summaries(max(limit, 500), pipeline=PIPELINE_FULL)
    local_by_job: dict[str, dict[str, Any]] = {}
    for item in items:
        batch_meta = item.get("gemini_batch") if isinstance(item.get("gemini_batch"), dict) else {}
        job_name = str(batch_meta.get("job_name") or "").strip()
        if job_name:
            local_by_job[job_name] = item

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

        local_item = local_by_job.get(job_name) or {}
        inferred_upload_id = _extract_upload_id_from_batch_obj(op)
        local_batch = local_item.get("gemini_batch") if isinstance(local_item.get("gemini_batch"), dict) else {}

        batch_status = _derive_ui_batch_status(
            live_state=live_state,
            done_flag=done_flag,
            error_obj=error_obj,
            local_status=local_batch.get("status"),
        )

        jobs.append(
            {
                "upload_id": local_item.get("upload_id"),
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
                "started_at": metadata.get("createTime") or local_batch.get("started_at"),
                "completed_at": metadata.get("endTime") or local_batch.get("completed_at"),
                "error": local_batch.get("error") or (json.dumps(error_obj, ensure_ascii=True) if error_obj else None),
            }
        )
        seen_job_names.add(job_name)
        if not jobs[-1].get("upload_id") and inferred_upload_id:
            jobs[-1]["upload_id"] = inferred_upload_id

    # Always merge in locally tracked jobs that may not appear in the
    # current remote page (or may be temporarily absent from remote listing).
    for job_name, local_item in local_by_job.items():
        if job_name in seen_job_names:
            continue
        local_batch = local_item.get("gemini_batch") if isinstance(local_item.get("gemini_batch"), dict) else {}
        jobs.append(
            {
                "upload_id": local_item.get("upload_id"),
                "upload_status": local_item.get("status"),
                "batch_status": _derive_ui_batch_status(
                    live_state="",
                    done_flag=False,
                    error_obj=None,
                    local_status=local_batch.get("status"),
                ),
                "job_name": job_name,
                "live_state": None,
                "done": None,
                "updated_at": local_item.get("updated_at"),
                "started_at": local_batch.get("started_at"),
                "completed_at": local_batch.get("completed_at"),
                "error": local_batch.get("error"),
            }
        )

    jobs.sort(key=lambda item: str(item.get("updated_at") or item.get("started_at") or ""), reverse=True)
    return {
        "count": len(jobs),
        "jobs": jobs,
        "source": "remote" if remote_error is None else "local_fallback",
        "remote_error": remote_error,
    }


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
                elif batch_status in {"succeeded", "running"}:
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
    job_name = str(gemini_batch_meta.get("job_name") or "").strip()
    if not job_name:
        raise HTTPException(status_code=400, detail="No Gemini batch job found for upload")

    try:
        cancel_resp = await asyncio.to_thread(_gemini_batch_cancel_sync, job_name)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini batch cancel failed: {str(exc)}") from exc

    async with get_upload_lock(upload_id):
        try:
            state = await read_upload_artifact(upload_id, "state")
            current_batch = state.get("gemini_batch") if isinstance(state.get("gemini_batch"), dict) else {}
            state["gemini_batch"] = {
                "status": "cancel_requested",
                "started_at": current_batch.get("started_at"),
                "completed_at": current_batch.get("completed_at"),
                "job_name": job_name,
                "error": None,
            }
            await persist_upload_state(upload_id, state)
        except Exception:
            pass

    return {
        "upload_id": upload_id,
        "job_name": job_name,
        "status": "cancel_requested",
        "response": cancel_resp,
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


@app.post("/batch/jobs/cancel")
async def batch_job_cancel_by_name(job_name: str = Query(..., min_length=1)) -> dict[str, Any]:
    try:
        cancel_resp = await asyncio.to_thread(_gemini_batch_cancel_sync, job_name)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini batch cancel failed: {str(exc)}") from exc
    return {
        "job_name": job_name,
        "status": "cancel_requested",
        "response": cancel_resp,
    }


@app.post("/batch/jobs/delete")
async def batch_job_delete_by_name(job_name: str = Query(..., min_length=1)) -> dict[str, Any]:
    try:
        delete_resp = await asyncio.to_thread(_gemini_batch_delete_sync, job_name)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini batch delete failed: {str(exc)}") from exc
    return {
        "job_name": job_name,
        "status": "deleted",
        "response": delete_resp,
    }


@app.get("/uploads/{upload_id}/status")
async def upload_status(upload_id: str) -> dict[str, Any]:
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

    try:
        state = await maybe_requeue_stuck_queued_rows(upload_id, state)
    except Exception:
        pass
    state = await maybe_fail_stale_processing_rows(upload_id, state)
    summary = summarize_upload_state(dict(state))
    return {
        "upload_id": summary["upload_id"],
        "pipeline": summary.get("pipeline") or PIPELINE_FULL,
        "status": summary["status"],
        "gemini_batch": summary.get("gemini_batch"),
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
        "file_links": _upload_file_links(upload_id),
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
                "s3_html_key": row.get("s3_html_key"),
                "s3_serpwow_json_key": row.get("s3_serpwow_json_key"),
            }
            for row in summary.get("rows", [])
        ],
    }


@app.get("/uploads/{upload_id}/failure-analysis")
async def upload_failure_analysis(
    upload_id: str,
    sample_limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
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
    try:
        state = await maybe_requeue_stuck_queued_rows(upload_id, state)
    except Exception:
        pass
    state = await maybe_fail_stale_processing_rows(upload_id, state)
    summary = summarize_upload_state(dict(state))
    return build_failure_analysis(summary, sample_limit=sample_limit)


@app.get("/uploads/{upload_id}/output")
async def upload_output(
    upload_id: str,
    download: bool = Query(False),
    format: Literal["json", "xlsx"] = Query("json"),
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


@app.get("/gmaps/discover")
async def gmaps_discover(q: str, country: Optional[str] = None) -> dict[str, Any]:
    started_monotonic = asyncio.get_running_loop().time()
    api_key = os.getenv("SERPWOW_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="SERPWOW_API_KEY is not configured")
    
    try:
        from app.services.serpwow import gmaps as gmaps_module
        import aiohttp
        gl = gmaps_module.country_to_gl(country) if country else gmaps_module.get_gl_from_query(q)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            cids = await gmaps_module.fetch_data_cids(session, q, gl_override=gl)
        return {
            "query": q,
            "gl": gl,
            "cids": cids,
            "processing_seconds": round(asyncio.get_running_loop().time() - started_monotonic, 3),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/gmaps/details")
async def gmaps_details(cid: str) -> dict[str, Any]:
    started_monotonic = asyncio.get_running_loop().time()
    api_key = os.getenv("SERPWOW_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="SERPWOW_API_KEY is not configured")
    
    try:
        from app.services.serpwow import gmaps as gmaps_module
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=30)
        sem = asyncio.Semaphore(1)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            detail = await gmaps_module.fetch_place_detail(session, cid, sem)
        response = dict(detail) if isinstance(detail, dict) else {"detail": detail}
        response["processing_seconds"] = round(asyncio.get_running_loop().time() - started_monotonic, 3)
        return response
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/gmaps/search")
async def gmaps_search(q: str, country: Optional[str] = None) -> dict[str, Any]:
    started_monotonic = asyncio.get_running_loop().time()
    api_key = os.getenv("SERPWOW_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="SERPWOW_API_KEY is not configured")
    
    try:
        from app.services.serpwow import gmaps as gmaps_module
        res = await gmaps_module.process_gmaps_query(q, country=country)
        response = dict(res) if isinstance(res, dict) else {"result": res}
        response["processing_seconds"] = round(asyncio.get_running_loop().time() - started_monotonic, 3)
        return response
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def build_selected_phase_queries(
    company_name: str,
    country: str,
    parsed_city_state: str = "",
    full_address: str = "",
    industry: str = "",
    phase: str = "all",
) -> list[tuple[str, str]]:
    attempt_queries: list[tuple[str, str]] = []
    seen_queries: set[str] = set()

    clean_company = _normalize_location_token(company_name)
    clean_country = _normalize_location_token(country)
    clean_city_state = _normalize_location_token(parsed_city_state)
    clean_industry = _normalize_location_token(industry)

    variants = _company_name_variants(clean_company)
    v_primary = variants[0] if variants else clean_company
    v_punct = variants[1] if len(variants) > 1 else v_primary
    v_compact = variants[2] if len(variants) > 2 else v_primary

    # Location markers and extraction
    plot_raw = _extract_address_component(full_address, ("plot",)) if full_address else ""
    road_raw = _extract_address_component(full_address, ("road", " rd")) if full_address else ""
    block_raw = _extract_address_component(full_address, ("block",)) if full_address else ""
    house_raw = _extract_address_component(full_address, ("house",)) if full_address else ""
    
    plot_hyphen, plot_space = _marker_variants(plot_raw) if plot_raw else ("", "")
    road_hyphen, road_space = _marker_variants(road_raw) if road_raw else ("", "")
    block_hyphen, _ = _marker_variants(block_raw) if block_raw else ("", "")
    house_hyphen, _ = _marker_variants(house_raw) if house_raw else ("", "")
    
    if not house_hyphen and plot_hyphen:
        number_match = re.search(r"\b(\d{1,5})\b", plot_hyphen)
        if number_match:
            house_hyphen = f"House-{number_match.group(1)}"

    locality, city, postal = _extract_locality_city_postal(full_address, clean_city_state, clean_country) if full_address else ("", "", "")
    location_phrase = " ".join(part for part in [locality, city] if part).strip() or clean_city_state or clean_country

    has_address = bool(full_address and full_address.strip())

    # Phase 1: Initial Hook & Punctuation Variations
    if phase in ("phase1", "all"):
        if has_address:
            if plot_hyphen and road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase1_exact_hook",
                    f'"{v_primary}" "{plot_hyphen}" "{road_hyphen}" {location_phrase}'.strip(),
                )
            if plot_space and road_space:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase1_punctuation_variation",
                    f'"{v_punct}" "{plot_space}" "{road_space}" {location_phrase}'.strip(),
                )
            if block_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase1_block_variation",
                    f'"{v_compact}" "{block_hyphen}" {location_phrase}'.strip(),
                )
            if city and postal:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase1_city_postal",
                    f'"{v_primary}" "{city} {postal}"',
                )
            elif location_phrase:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase1_city_country",
                    f'"{v_primary}" "{location_phrase}"',
                )
        else:
            # Address is not available, only Country is available
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase1_no_address_primary",
                f'"{v_primary}" {clean_country}',
            )
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase1_no_address_punct",
                f'"{v_punct}" {clean_country}',
            )
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase1_no_address_compact",
                f'"{v_compact}" official site {clean_country}',
            )

    # Phase 2: AI Overview Natural Language Prompts
    if phase in ("phase2", "all"):
        if has_address:
            if plot_space and road_space:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase2_business_name_at_address",
                    (
                        f'What business or trade name operates under "{v_primary}" at '
                        f"{plot_space}, {road_space}, {location_phrase}?"
                    ),
                )
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase2_exec_or_owner",
                f'Who is the owner, CEO, or Managing Director of "{v_primary}" located in {location_phrase}?',
            )
            if block_hyphen and plot_hyphen and road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase2_registered_companies",
                    (
                        "What companies are registered at the address "
                        f'{block_hyphen}, {plot_hyphen}, {road_hyphen}, {location_phrase}?'
                    ),
                )
            if road_space:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase2_consumer_website",
                    (
                        f"Is there a consumer-facing website for {v_primary} "
                        f"registered at {road_space} {location_phrase}?"
                    ),
                )
        else:
            # Address not available
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase2_no_address_owner",
                f'Who is the owner, CEO, or Managing Director of "{v_primary}" in {clean_country}?',
            )
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase2_no_address_domain",
                f'What is the official website or domain of "{v_primary}" in {clean_country}?',
            )

    # Phase 3: Address-Only Pivot Discovery
    if phase in ("phase3", "all"):
        if has_address:
            if plot_hyphen and road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase3_address_pivot",
                    f'"{plot_hyphen}" "{road_hyphen}" {location_phrase} -residential',
                )
            if house_hyphen and road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase3_house_plot_variation",
                    f'"{house_hyphen}" "{road_hyphen}" {location_phrase} company OR business',
                )
            if block_hyphen and plot_hyphen and road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase3_block_plot_road",
                    f'"{block_hyphen}" "{plot_hyphen}" "{road_hyphen}" {location_phrase}',
                )
        else:
            # Address not available
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase3_no_address_registry",
                f'"{v_primary}" registry database {clean_country}',
            )

    # Phase 4: Document Hunting (Filetype/Registry Dorks)
    if phase in ("phase4", "all"):
        country_tld = _country_to_gl(clean_country)
        _append_unique_attempt_query(
            attempt_queries,
            seen_queries,
            "phase4_country_registry_docs",
            f'site:.{country_tld} "{v_primary}" "{(locality or city or clean_country)}"',
        )
        if has_address:
            if plot_hyphen or road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase4_company_plot_road_docs",
                    (
                        f'filetype:pdf "{v_primary}" '
                        f'"{plot_hyphen or plot_space or plot_raw}" OR "{road_hyphen or road_space or road_raw}"'
                    ),
                )
            if plot_hyphen and road_hyphen:
                _append_unique_attempt_query(
                    attempt_queries,
                    seen_queries,
                    "phase4_address_directory_docs",
                    f'filetype:pdf "{plot_hyphen}" "{road_hyphen}" {(locality or city or clean_country)} directory',
                )
        else:
            # Address not available
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "phase4_no_address_docs",
                f'filetype:pdf "{v_primary}" registry OR profile {clean_country}',
            )

    # Fallback Searches
    if phase in ("fallback", "all"):
        if clean_industry:
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "industry_fallback",
                build_industry_fallback_query(clean_company, clean_industry),
            )
        else:
            _append_unique_attempt_query(
                attempt_queries,
                seen_queries,
                "simple_fallback",
                f"{v_primary} {clean_country}",
            )

    return attempt_queries


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
