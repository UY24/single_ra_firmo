# backend/app/services/serpwow/gemini_llm.py
"""Gemini generate-content helpers + final-URL confidence/selection for SerpWow."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.services.serpwow.url_utils import (
    _normalize_url_for_compare,
    _official_website_looks_plausible,
)

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


def _gemini_generate_content_json(
    model: str, prompt: str, timeout: float = 45.0
) -> tuple[Optional[str], Optional[dict[str, Any]], Optional[str]]:
    """Single Gemini generateContent call returning (text, usage_metadata, error).

    Isolated so the confidence step is unit-testable without network (tests patch
    this). Mirrors single_ra's inline urlopen call.
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None, None, "GEMINI_API_KEY not configured"
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    req = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        return None, None, f"Gemini HTTPError: {exc.code}"
    except URLError as exc:
        return None, None, f"Gemini URLError: {exc.reason}"
    except Exception as exc:
        return None, None, f"Gemini error: {exc}"
    try:
        response_json = json.loads(body)
        text = (
            response_json.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        usage_metadata = response_json.get("usageMetadata", {})
        return text, usage_metadata, None
    except Exception:
        return None, None, "Gemini response parse error"


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
    """LLM domain-validation + confidence scoring. Selects from candidate_urls ONLY,
    never invents; rejects out-of-set / implausible picks to score 0. Ported from
    single_ra.app.choose_final_website_with_gemini."""
    configured_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    ordered_models: list[str] = []
    for model_name in (configured_model, "gemini-2.5-flash-lite"):
        if model_name and model_name not in ordered_models:
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

    last_error: Optional[str] = None
    for model in ordered_models:
        text, usage_metadata, err = _gemini_generate_content_json(model, prompt)
        if err:
            last_error = err
            if "404" in err:
                continue
            return None, err, model, usage_metadata

        parsed = _parse_json_from_text(text or "")
        if not parsed:
            return None, "Gemini returned non-JSON output", model, usage_metadata

        try:
            confidence_score = int(float(parsed.get("confidence_score", 0)))
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
                # Hard reject: the LLM invented a URL the search never returned.
                normalized["official_website"] = None
                normalized["confidence_score"] = 0
                normalized["confidence"] = "low"
                normalized["reason"] = (
                    f"Rejected URL outside candidate set: {selected_norm or selected.strip()}"
                )
            else:
                # Keep the in-candidate pick. The domain-token heuristic is crude
                # (brand/abbreviation domains fail it), so it no longer DROPS the
                # URL — it only flags it for review so nothing correct is lost.
                normalized["official_website"] = selected_norm
                if not _official_website_looks_plausible(selected_norm, company_name, country):
                    normalized["domain_name_mismatch"] = True
        return normalized, None, model, usage_metadata

    return None, (last_error or "Gemini model resolution failed"), configured_model, None
