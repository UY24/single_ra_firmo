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
    is_disallowed_official_url,
    url_matches_domain,
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


def build_ai_overview_prompt(
    company_name: str,
    country: str,
    official_website: str,
    ai_overview: dict[str, Any],
) -> str:
    """The firmographics normalisation prompt, for BOTH transports.

    Extracted 2026-08-19 when batch mode landed: the batch path builds its request in
    ``engine._build_batch_prompt_for_row``, and a second copy of this text is how inline and
    batched runs would start answering the same row differently.
    """
    return (
        "You are a data normalization system.\n"
        "Convert the provided Google AI Overview into strict JSON only.\n"
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
        "Google AI Overview JSON:\n"
        # Truncated because a rich overview can be far larger than the answer needs.
        f"{json.dumps(ai_overview, ensure_ascii=True)[:14000]}"
    )


def standardize_ai_overview_with_gemini(
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

    prompt = build_ai_overview_prompt(
        company_name, country, official_website, ai_overview)

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


def build_relationship_prompt(
    x_name: str,
    y_name: str,
    input_url: str,
    candidates: list[str],
    ai_overview_evidence: list[dict[str, Any]],
    search_attempts: list[dict[str, Any]],
    x_domain: str = "",
) -> str:
    """Verdict prompt for the relationship pipeline's Gemini Batch phase.

    The row has three inputs and no location: Company X, Company Y and X's portfolio
    page (x_domain is derived from that URL).
    """
    input_obj = {
        "company_x": x_name, "company_x_domain": x_domain or None,
        "company_x_official_portfolio_page": input_url,
        "company_y": y_name,
    }
    evidence_sections: list[str] = []
    for item in ai_overview_evidence or []:
        if not isinstance(item, dict):
            continue
        lines = [f"[{item.get('phase') or 'unknown_phase'}]"]
        if item.get("query"):
            lines.append(f"Query: {item['query']}")
        if item.get("text"):
            lines.append(str(item["text"]))
        sources = item.get("sources") if isinstance(item.get("sources"), list) else []
        if sources:
            lines.append("Sources:")
            for source in sources:
                if isinstance(source, dict) and source.get("url"):
                    lines.append(f"- {source.get('name') or source['url']}: {source['url']}")
        evidence_sections.append("\n".join(lines))
    evidence_text = "\n\n".join(evidence_sections)
    return (
        "You verify FINANCIAL relationships between companies and identify official websites.\n"
        "company_y is OCR-derived text from a logo on company_x's official portfolio page;\n"
        "it may be garbled, truncated, or contain noise around\n"
        "the real company name (e.g. 'YUZU SPARKLINGWE SANZO POMELO' contains 'SANZO').\n"
        "Return strict JSON only with this schema:\n"
        "{\n"
        '  "relationship_status": "confirmed"|"not_confirmed"|"unclear",\n'
        '  "relationship_summary": string,\n'
        '  "relationship_evidence": [string],\n'
        '  "official_website": string|null,\n'
        '  "relationship_confidence_score": number,\n'
        '  "website_confidence_score": number,\n'
        '  "reason": string,\n'
        '  "extra_flags": [string]\n'
        "}\n"
        "Rules:\n"
        "- GROUND EVERYTHING IN THE SUPPLIED MATERIAL. Your job is to READ the evidence\n"
        "  below and report what it says — not to answer from your own knowledge of these\n"
        "  companies. Do not use anything you know that is not in the supplied evidence, do\n"
        "  not infer, do not assume, and do not fill gaps. If the evidence does not settle a\n"
        "  field, say so through the schema (null, or not_confirmed/unclear with a low\n"
        "  score) rather than producing a plausible answer. Two runs given the same evidence\n"
        "  must reach the same verdict.\n"
        "- Every claim in relationship_summary and relationship_evidence must be traceable\n"
        "  to a specific statement in the evidence below. Do not paraphrase into something\n"
        "  stronger than the source, and do not add facts the source does not state.\n"
        "- relationship_status is about a FINANCIAL relationship only (investment, portfolio\n"
        "  company, funding round, acquisition, fund backing). Mere similarity or co-mention\n"
        "  without financial context is NOT a relationship.\n"
        "- 'confirmed' requires explicit supporting evidence in the provided material.\n"
        "- official_website must be company_y's site, chosen from Candidate URLs ONLY.\n"
        "  Never invent a URL. Never return company_x's own website.\n"
        "- company_x_domain (when present) is company_x's own website domain — never return it as official_website.\n"
        "- Set official_website to null unless relationship_status is 'confirmed'.\n"
        "- Never return directory/listing/social/wiki/news/search/file URLs.\n"
        "- relationship_confidence_score is 0-100 = how strongly the provided evidence\n"
        "  (AI Mode text/sources) shows a FINANCIAL relationship EXISTS between company_x\n"
        "  and company_y: 0 = no relationship or no evidence, 100 = clearly evidenced. This is\n"
        "  confidence that the relationship is real, NOT confidence in your verdict — so a\n"
        "  not_confirmed verdict must carry a LOW score.\n"
        "- website_confidence_score is 0-100 for the Company Y URL; use 0 when no URL is found.\n"
        "- relationship_summary: 1-2 sentences quoting what the evidence says.\n"
        "- relationship_evidence: at most 3 concise statements from the supplied evidence.\n"
        "- extra_flags: optional short slugs like \"company_closed\" (evidence says company\n"
        "  shut down) or \"ocr_name_suspicious\" (the OCR text may name a different company).\n\n"
        f"Input: {json.dumps(input_obj, ensure_ascii=True)}\n\n"
        f"Candidate URLs: {json.dumps(list(candidates or []), ensure_ascii=True)}\n\n"
        # Every string the provider's response contained, in order — not a rendering of the
        # keys we happen to know about. AI Mode structures the same question differently from
        # call to call (sometimes headings, sometimes one ordered_list, sometimes a single
        # paragraph), so the answer's own sections are the only structure there is.
        "AI Mode evidence — every line Google's answer contained, in order. Section\n"
        "labels, prose, evidence bullets and any URL it cited all appear as plain lines:\n"
        f"{evidence_text}\n\n"
        f"Search attempts: {json.dumps(list(search_attempts or []), ensure_ascii=True)[:6000]}"
    )


_VALID_REL_STATUSES = {"confirmed", "not_confirmed", "unclear"}


def update_relationship_block(
    relationship: dict[str, Any],
    parsed: dict[str, Any],
    status: str,
    gate_flags: list[dict[str, str]],
) -> dict[str, Any]:
    relationship["status"] = status
    relationship["summary"] = str(parsed.get("relationship_summary") or "")
    evidence = parsed.get("relationship_evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    elif not isinstance(evidence, list):
        evidence = []
    relationship["evidence"] = [str(item) for item in evidence[:3]
                                if str(item).strip()]
    relationship["relationship_confidence_score"] = int(
        parsed.get("relationship_confidence_score") or 0)
    relationship["website_confidence_score"] = int(
        parsed.get("website_confidence_score") or 0)
    flags = relationship.get("flags") if isinstance(relationship.get("flags"), list) else []
    flags.extend(gate_flags)
    extra_flags = parsed.get("extra_flags") or []
    if isinstance(extra_flags, str):
        extra_flags = [extra_flags]
    for extra in extra_flags if isinstance(extra_flags, list) else []:
        if isinstance(extra, str) and extra.strip():
            flags.append({"flag": extra.strip(), "why": "reported by LLM"})
    relationship["flags"] = flags
    return relationship


def apply_relationship_gate(
    parsed: dict[str, Any],
    candidates: list[str],
    x_domain: str,
) -> tuple[Optional[str], str, list[dict[str, str]]]:
    """Code-enforced validation of a relationship LLM output (spec §4 rules 1-4).

    Returns (gated_url, relationship_status, gate_flags). gated_url is non-None
    ONLY when status == "confirmed" AND the URL is in the candidate set AND not
    disallowed AND not on Company X's own domain. A URL that exists but fails
    the gate is preserved in a flag so the evidence isn't lost.
    """
    parsed = parsed if isinstance(parsed, dict) else {}
    status = str(parsed.get("relationship_status") or "").strip().lower()
    if status not in _VALID_REL_STATUSES:
        status = "unclear"
    parsed["relationship_status"] = status
    flags: list[dict[str, str]] = []

    def _score(value: Any) -> int:
        try:
            return max(0, min(100, int(float(value or 0))))
        except (TypeError, ValueError):
            return 0

    legacy_score = _score(parsed.get("confidence_score"))
    relationship_score = _score(
        parsed.get("relationship_confidence_score", legacy_score))
    website_score = _score(parsed.get("website_confidence_score", legacy_score))
    parsed["relationship_confidence_score"] = relationship_score
    parsed["website_confidence_score"] = website_score

    raw_url = parsed.get("official_website")
    url = raw_url.strip() if isinstance(raw_url, str) and raw_url.strip() else None
    if url is None:
        parsed["website_confidence_score"] = 0

    if url is not None:
        normalized_candidates = {
            _normalize_url_for_compare(c) for c in (candidates or []) if str(c or "").strip()
        }
        normalized_candidates.discard("")
        if _normalize_url_for_compare(url) not in normalized_candidates:
            flags.append({"flag": "llm_url_out_of_candidates",
                          "why": f"LLM returned {url} which is not in the candidate set"})
            url = None
            parsed["website_confidence_score"] = 0
    if url is not None and is_disallowed_official_url(url):
        flags.append({"flag": "disallowed_url_dropped",
                      "why": f"{url} is a directory/social/file URL"})
        url = None
        parsed["website_confidence_score"] = 0
    if url is not None and url_matches_domain(url, x_domain):
        flags.append({"flag": "x_domain_candidate_dropped",
                      "why": f"{url} is Company X's own site, never Y's"})
        url = None
        parsed["website_confidence_score"] = 0

    if status != "confirmed" and url is not None:
        flag_name = ("relationship_unclear" if status == "unclear"
                     else "url_found_no_relationship")
        flags.append({"flag": flag_name,
                      "why": f"candidate URL {url} found but relationship is {status}"})
        url = None
    elif status == "unclear":
        flags.append({"flag": "relationship_unclear",
                      "why": "evidence neither confirms nor rules out a financial relationship"})

    if status == "confirmed" and url is None:
        flags.append({"flag": "relationship_confirmed_url_missing",
                      "why": "financial relationship confirmed but no valid Company Y URL passed the evidence gate"})

    parsed["confidence_score"] = (
        min(relationship_score, parsed["website_confidence_score"])
        if status == "confirmed" and url is not None else 0
    )

    return url, status, flags
