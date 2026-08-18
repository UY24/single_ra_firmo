# backend/app/services/serpwow/csv_input.py
"""Upload CSV parsing/validation for the SerpWow pipelines."""
from __future__ import annotations

import csv
import io
import re

from fastapi import HTTPException

from app.services.serpwow.url_utils import _domain_from_url, _normalize_website_input


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
            "CSV must include headers for firmographics upload. "
            "Required: website_url (or official_website/website/url/domain)."
        )

    normalized = {_normalize_header(h): h for h in reader.fieldnames if h}
    website_key = None
    company_key = None
    country_key = None
    firm_id_key = None
    industry_key = None
    full_address_key = None

    # website_url first: it is what every other pipeline WRITES into found.csv, so an
    # enrichment run can take that file back unchanged. The rest are legacy aliases.
    for key in ("website_url", "official_website", "website", "url", "domain"):
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
            "Firmographics CSV must include a website_url "
            "(or official_website/website/url/domain) column."
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
        raise ValueError("Firmographics CSV has no valid rows with a website_url.")
    return rows
