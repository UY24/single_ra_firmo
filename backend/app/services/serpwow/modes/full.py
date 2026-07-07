# backend/app/services/serpwow/modes/full.py
"""full / url_discovery mode executor (company_lookup)."""
from __future__ import annotations

import asyncio
import httpx
import json
import re
from typing import Any, Optional
from urllib.parse import urlparse

from app.services.serpwow.schemas import CrawlResponse
from app.services.common.env import (
    get_bool_env as _get_bool_env,
    get_int_env as _get_int_env,
    get_float_env as _get_float_env,
)

from app.services.serpwow.address import (
    _address_evidence_markers,
    _dedupe_location_parts,
    _extract_address_component,
    _heuristic_city_state_from_full_address,
    _is_address_aligned,
    _is_suspicious_city_state_value,
    _marker_variants,
    _normalize_location_token,
)
from app.services.serpwow.constants import (
    PIPELINE_URL_DISCOVERY,
)
from app.services.serpwow.cost import (
    calculate_gemini_cost_usd,
    calculate_serpwow_cost_usd,
)
from app.services.serpwow.gemini_llm import (
    choose_final_website_with_gemini,
    classify_address_with_gemini,
    parse_city_state_from_full_address_with_gemini,
    standardize_serpwow_ai_overview_with_gemini,
    transliterate_inputs_with_gemini,
)
from app.services.serpwow.query_builders import (
    _extract_phase5_pivots_from_serpwow,
    build_address_fallback_query,
    build_investigative_search_queries,
)
from app.services.serpwow.row_logging import (
    _log_row_stage,
    _short_text,
)
from app.services.serpwow.serpwow_client import (
    run_serpwow_search,
)
from app.services.serpwow.url_utils import (
    _candidate_domain_is_plausible_for_company,
    _normalized_domain,
    _official_website_looks_plausible,
    is_disallowed_official_url,
)
from app.services.serpwow.modes.common import (
    run_gmaps_from_module,
    run_serpwow_from_codetails,
)

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
