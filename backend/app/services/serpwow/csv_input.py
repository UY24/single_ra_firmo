# backend/app/services/serpwow/csv_input.py
"""Upload CSV parsing/validation for the SerpWow pipelines."""
from __future__ import annotations

import csv
import io
import re

from fastapi import HTTPException

from app.services.serpwow.url_utils import _normalize_website_input


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


# Firmographics input columns, canonical name -> accepted headers, first match wins.
# website_url leads because that is the name every other pipeline WRITES into found.csv,
# so an enrichment run can take that file back unchanged.
_FIRMO_ALIASES = {
    "website_url": ("website_url", "official_website", "website", "url", "domain"),
    # Same list parse_entities_csv accepts, so a found.csv round-trips with its
    # company names intact instead of falling back to the domain.
    "company_name": ("company_name", "company", "name", "entity_name", "entity",
                     "organization", "organisation", "legal_name"),
    "country": ("country", "country_name", "nation"),
    "firm_id": ("firm_id", "firmid", "id"),
    "industry": ("industry", "input_industry"),
    "full_address": ("full_address", "address", "fulladdress", "input_full_address"),
}


def firmographics_columns(fieldnames) -> dict[str, str]:
    """Canonical field -> the CSV header that supplied it, for the headers present.

    Shared by the parser and the New Run preview, so the preview cannot claim a mapping
    the upload will not use — the failure that made a firmographics CSV render as
    "col 1 = company name, col 2 = country".
    """
    normalized = {_normalize_header(h): h for h in (fieldnames or []) if h}
    resolved: dict[str, str] = {}
    for canonical, aliases in _FIRMO_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                resolved[canonical] = normalized[alias]
                break
    return resolved


def parse_firmographics_csv_rows(raw: bytes,
                                 sample_limit: int | None = None) -> list[dict[str, str]]:
    """Validate every row; keep at most ``sample_limit`` of them.

    ``sample_limit=0`` validates and counts without retaining anything -- what the S3-only
    upload path wants, since it ships the raw bytes to S3 and the worker streams them back.
    Materialising 500k row dicts in the API process was ~100MB per upload that no caller
    read. Same convention as ``parse_entities_csv``; use ``count_firmographics_csv_rows``
    when only the number is wanted.
    """
    text = raw.decode("utf-8-sig", errors="replace")
    stream = io.StringIO(text)
    reader = csv.DictReader(stream)

    rows: list[dict[str, str]] = []
    total_valid = 0
    if not reader.fieldnames:
        raise ValueError(
            "CSV must include headers for firmographics upload. "
            "Required: website_url (or official_website/website/url/domain)."
        )

    columns = firmographics_columns(reader.fieldnames)
    website_key = columns.get("website_url")
    company_key = columns.get("company_name")
    country_key = columns.get("country")
    firm_id_key = columns.get("firm_id")
    industry_key = columns.get("industry")
    full_address_key = columns.get("full_address")

    if not website_key:
        raise ValueError(
            "Firmographics CSV must include a website_url "
            "(or official_website/website/url/domain) column."
        )

    for idx, row in enumerate(reader, start=1):
        official_website = _normalize_website_input(str(row.get(website_key) or ""))
        if not official_website:
            continue
        # Whatever the file has, nothing more: a name derived from the domain reads
        # like real data in the output and isn't.
        total_valid += 1
        if sample_limit is not None and len(rows) >= sample_limit:
            continue
        company_name = (row.get(company_key) or "").strip() if company_key else ""
        country = (row.get(country_key) or "").strip() if country_key else ""
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

    if not total_valid:
        raise ValueError("Firmographics CSV has no valid rows with a website_url.")
    return rows


def count_firmographics_csv_rows(raw: bytes) -> int:
    """How many rows the worker will process. Validates every row, retains none.

    The S3-only upload path needs the count for status.json and the Supabase run row, and
    nothing else -- the worker streams input.csv straight back out of S3.
    """
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError(
            "CSV must include headers for firmographics upload. "
            "Required: website_url (or official_website/website/url/domain)."
        )
    website_key = firmographics_columns(reader.fieldnames).get("website_url")
    if not website_key:
        raise ValueError(
            "Firmographics CSV must include a website_url "
            "(or official_website/website/url/domain) column."
        )
    total = sum(1 for row in reader
                if _normalize_website_input(str(row.get(website_key) or "")))
    if not total:
        raise ValueError("Firmographics CSV has no valid rows with a website_url.")
    return total
