"""
serpwow_gmaps.py
----------------
Self-contained SerpWow Google Maps + Place Details pipeline.

Exports
-------
- fetch_data_cids(session, q)          -> List[str]
- fetch_place_detail(session, cid, sem) -> dict
- process_gmaps_query(q)               -> dict
- filter_place_details(data)           -> dict
- extract_gmaps_website(gmaps_results) -> str | None
- get_gl_from_query(q)                 -> str
- detect_country_from_query(q)         -> str
- country_to_gl(country_input)         -> str
"""

import asyncio
import json
import os
import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    import yaml
except ImportError:
    yaml = None

# ---------------------------------------------------------------------------
# Config helpers (mirrors original pattern; keeps module self-contained)
# ---------------------------------------------------------------------------

def _load_yaml_config(path: str) -> dict:
    if yaml is None or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _cfg(data: dict, path: str, default=None):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


# Read config once at import time
_CONFIG_FILE = os.getenv("APP_CONFIG_FILE", "config.yaml").strip() or "config.yaml"
_CONFIG = _load_yaml_config(_CONFIG_FILE)

# ---------------------------------------------------------------------------
# Runtime settings (pulled from env / config; mirrors service_config pattern)
# ---------------------------------------------------------------------------

SERPWOW_API_KEY: str = os.getenv("SERPWOW_API_KEY", "")
SERPWOW_BASE_URL: str = os.getenv(
    "SERPWOW_BASE_URL", "https://api.serpwow.com/live/search"
)
SERPWOW_USD_PER_SEARCH: float = float(os.getenv("SERPWOW_USD_PER_SEARCH", "0.0"))

# Fields to keep from each place_details response
PLACE_FIELDS: set = {
    str(f).strip()
    for f in (_cfg(_CONFIG, "serpwow.place_fields", []) or [])
    if str(f).strip()
}

# Country name → Google gl code mapping from config
COUNTRY_TO_GL: Dict[str, str] = {
    str(k).strip().lower(): str(v).strip().lower()
    for k, v in (_cfg(_CONFIG, "country_to_gl", {}) or {}).items()
    if str(k).strip() and str(v).strip()
}

# ---------------------------------------------------------------------------
# Cost tracking (scoped to this module)
# ---------------------------------------------------------------------------

_RUN_COST_SERPWOW = {
    "searches": 0,
    "rate_usd_per_search": SERPWOW_USD_PER_SEARCH,
    "total_cost_usd": 0.0,
}


def _record_serpwow_search() -> None:
    _RUN_COST_SERPWOW["searches"] += 1
    _RUN_COST_SERPWOW["total_cost_usd"] = round(
        _RUN_COST_SERPWOW["searches"] * _RUN_COST_SERPWOW["rate_usd_per_search"], 8
    )


def get_serpwow_cost_summary() -> dict:
    """Return a copy of the current SerpWow cost counters."""
    return dict(_RUN_COST_SERPWOW)


# ---------------------------------------------------------------------------
# Country / GL helpers
# ---------------------------------------------------------------------------

def detect_country_from_query(q: str) -> str:
    """Best-effort country detection from free-text query string."""
    q_lower = (q or "").lower()

    for country in COUNTRY_TO_GL:
        if country in q_lower:
            return country

    if re.search(r"\b\d{2}-\d{3}\b", q or ""):
        return "poland"
    if re.search(r"\b\d{6}\b", q or ""):
        return "india"
    if re.search(r"\b\d{4,5}\b", q or ""):
        return "germany"

    return "united states"


def country_to_gl(country_input: Optional[str]) -> str:
    """Convert a country name or 2-letter code to a Google *gl* parameter."""
    if not country_input:
        return "us"
    key = country_input.strip().lower()
    if key in COUNTRY_TO_GL:
        return COUNTRY_TO_GL[key]
    if len(key) == 2:
        return key
    for name, code in COUNTRY_TO_GL.items():
        if key in name or name in key:
            return code
    return "us"


def get_gl_from_query(q: str) -> str:
    """Derive the Google *gl* parameter from a free-text query."""
    return country_to_gl(detect_country_from_query(q))


# ---------------------------------------------------------------------------
# Place-fields filter
# ---------------------------------------------------------------------------

def filter_place_details(data: dict) -> dict:
    """
    Keep only the keys listed in PLACE_FIELDS from the place_details payload.
    If PLACE_FIELDS is empty the full place_details dict is returned as-is.
    """
    pd = data.get("place_details", {})
    if not PLACE_FIELDS:
        return pd
    return {k: pd[k] for k in PLACE_FIELDS if k in pd}


# ---------------------------------------------------------------------------
# Low-level HTTP helper
# ---------------------------------------------------------------------------

async def _serpwow_get_json(session, params: dict) -> dict:
    """
    Fire a GET request to the SerpWow API and return the parsed JSON body.
    Records the search against the cost counter before every real HTTP call.
    """
    _record_serpwow_search()
    try:
        async with session.get(SERPWOW_BASE_URL, params=params) as resp:
            raw_text = await resp.text()
            status = resp.status
    except asyncio.TimeoutError:
        return {"_request_error": "timeout", "_http_status": None}
    except Exception as exc:
        return {"_request_error": f"{type(exc).__name__}: {exc}", "_http_status": None}

    try:
        payload = json.loads(raw_text)
        if isinstance(payload, dict):
            payload.setdefault("_http_status", status)
        return payload
    except Exception:
        return {
            "_parse_error": True,
            "_http_status": status,
            "_raw": raw_text[:2000],
        }


# ---------------------------------------------------------------------------
# GMaps: CID discovery
# ---------------------------------------------------------------------------

async def fetch_data_cids(session, q: str, gl_override: Optional[str] = None) -> List[str]:
    """
    Run a SerpWow *places* search and return the unique data_cid values found.

    Parameters
    ----------
    session : aiohttp.ClientSession
    q       : search query string (company name + location)

    Returns
    -------
    list of str  (empty on error)
    """
    gl = (gl_override or "").strip().lower() or get_gl_from_query(q)
    params = {
        "api_key": SERPWOW_API_KEY,
        "engine": "google",
        "search_type": "places",
        "google_domain": "google.com",
        "gl": gl,
        "hl": "en",
        "q": q,
    }
    data = await _serpwow_get_json(session, params)
    if data.get("_parse_error") or data.get("_request_error"):
        return []

    return list(
        {
            place.get("data_cid")
            for place in data.get("places_results", [])
            if place.get("data_cid")
        }
    )


# ---------------------------------------------------------------------------
# GMaps: place detail fetch
# ---------------------------------------------------------------------------

async def fetch_place_detail(
    session, cid: str, sem: asyncio.Semaphore
) -> dict:
    """
    Fetch full place details for a single *data_cid* via SerpWow.

    Concurrency is controlled by the caller-supplied semaphore.

    Returns a filtered dict of place fields on success, or an error dict.
    """
    async with sem:
        params = {
            "api_key": SERPWOW_API_KEY,
            "engine": "google",
            "search_type": "place_details",
            "data_cid": cid,
            "hl": "en",
        }
        try:
            _record_serpwow_search()
            async with session.get(SERPWOW_BASE_URL, params=params) as resp:
                raw_text = await resp.text()
            try:
                data = json.loads(raw_text)
            except Exception as parse_err:
                return {
                    "data_cid": cid,
                    "error": f"JSON parse failed: {parse_err}",
                    "raw": raw_text[:500],
                }

            if not data.get("request_info", {}).get("success", False):
                return {
                    "data_cid": cid,
                    "error": "API reported failure",
                    "request_info": data.get("request_info"),
                }

            return filter_place_details(data)

        except asyncio.TimeoutError:
            return {
                "data_cid": cid,
                "error": "Request timed out",
                "error_type": "TimeoutError",
            }
        except Exception as exc:
            return {
                "data_cid": cid,
                "error": str(exc) or repr(exc),
                "error_type": type(exc).__name__,
            }


# ---------------------------------------------------------------------------
# GMaps: high-level orchestrator
# ---------------------------------------------------------------------------

async def process_gmaps_query(q: str, country: Optional[str] = None) -> dict:
    """
    Full GMaps pipeline: places search → parallel place-detail fetch.

    Parameters
    ----------
    q : search query string

    Returns
    -------
    dict with keys:
        query        – original query
        gl           – Google geo-locale used
        total_places – number of data_cids found
        results      – list of place-detail dicts (or error dicts)
    """
    if not SERPWOW_API_KEY:
        return {
            "query": q,
            "gl": country_to_gl(country),
            "request_count": 0,
            "error": "SERPWOW_API_KEY missing",
            "results": [],
        }
    if aiohttp is None:
        return {
            "query": q,
            "gl": country_to_gl(country),
            "request_count": 0,
            "error": "aiohttp not installed",
            "results": [],
        }

    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=100)
    gl = country_to_gl(country) if country else get_gl_from_query(q)
    request_count = 0

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        data_cids = await fetch_data_cids(session, q, gl_override=gl)
        # One places search call was attempted.
        request_count += 1

        sem = asyncio.Semaphore(20)
        tasks = [fetch_place_detail(session, cid, sem) for cid in data_cids]
        # One place_details call per CID.
        request_count += len(tasks)
        results = await asyncio.gather(*tasks) if tasks else []

    return {
        "query": q,
        "gl": gl,
        "request_count": request_count,
        "total_places": len(data_cids),
        "results": list(results),
    }


# ---------------------------------------------------------------------------
# Utility: extract website URL from GMaps results
# ---------------------------------------------------------------------------

def clean_url_for_report(url: str) -> str:
    """Strip fragment/text anchors that pollute stored URLs."""
    value = (url or "").strip()
    if not value:
        return ""
    value = re.sub(r"#:~:text=.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"%23:~:text=.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"#.*$", "", value)
    return value.strip()


def extract_gmaps_website(gmaps_results: dict) -> Optional[str]:
    """
    Walk the place-detail results and return the first non-empty *website* field.

    Parameters
    ----------
    gmaps_results : the dict returned by :func:`process_gmaps_query`

    Returns
    -------
    str | None
    """
    results = (gmaps_results or {}).get("results") or []
    for item in results:
        if not isinstance(item, dict):
            continue
        website = clean_url_for_report(item.get("website") or "")
        if website:
            return website
    return None


# ---------------------------------------------------------------------------
# Build GMaps query from input context (convenience helper)
# ---------------------------------------------------------------------------

def build_gmaps_query(
    input_context: dict,
    rewrite_context: dict = None,
) -> str:
    """
    Compose a Google Maps search string from structured input context.

    Uses transliterated / rewritten values when available.
    """
    ctx = input_context or {}
    rw = rewrite_context or {}

    company = (
        (rw.get("transliterated_company_name") or "").strip()
        or (ctx.get("company_name") or "").strip()
    )
    location = (
        (rw.get("transliterated_address") or "").strip()
        or (rw.get("fallback_location") or "").strip()
        or (ctx.get("full_address") or "").strip()
        or " ".join(
            [(ctx.get("city") or "").strip(), (ctx.get("state") or "").strip()]
        ).strip()
        or (ctx.get("country") or "").strip()
    )
    industry = (ctx.get("industry") or "").strip()

    parts = [company, location, industry, "google maps"]
    q = " ".join([p for p in parts if p]).strip()
    q = re.sub(r"\s+", " ", q)
    q = q.replace("?", "").replace("What is the official website of ", "").strip()
    return q


# ---------------------------------------------------------------------------
# Standalone CLI (quick smoke-test)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="SerpWow GMaps pipeline smoke-test")
    parser.add_argument("query", help="GMaps search query")
    args = parser.parse_args()

    if not SERPWOW_API_KEY:
        print("ERROR: SERPWOW_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    gmaps_result = asyncio.run(process_gmaps_query(args.query))
    print(json.dumps(gmaps_result, ensure_ascii=False, indent=2))
    print("\n--- website ---")
    print(extract_gmaps_website(gmaps_result))
    print("\n--- cost ---")
    print(json.dumps(get_serpwow_cost_summary(), indent=2))
