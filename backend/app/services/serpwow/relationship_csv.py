"""CSV parsing for the relationship pipeline (spec §2).

Input: OCR-results CSV with required Input_URL, Company_Name_X, and
Company_Name_Y values on every row, optional city/country, and arbitrary
passthrough columns. Every row is processed independently — one row in, one row
out (no (X, Y) deduplication). Each row is still represented as a "pair" carrying
its single source row index so the downstream engine/reporting machinery is
unchanged (source_row_indices is always a 1-element list).
"""
from __future__ import annotations

import csv
import io
import re


class InvalidRelationshipCSV(ValueError):
    """Structural CSV problem — mapped to HTTP 400 by the upload endpoint."""


def _normalize_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


_Y_ALIASES = ("company_name_y", "company_y")
_X_ALIASES = ("company_name_x", "company_x")
_URL_ALIASES = ("input_url",)
_CITY_ALIASES = ("city", "town")
_COUNTRY_ALIASES = ("country", "country_name", "nation")


def _find_column(normalized: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return None


def parse_relationship_csv(raw: bytes) -> dict:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise InvalidRelationshipCSV("Empty CSV — no header row found.")
    header = [h for h in reader.fieldnames if h is not None]
    normalized = {_normalize_header(h): h for h in header if h}

    y_col = _find_column(normalized, _Y_ALIASES)
    if y_col is None:
        raise InvalidRelationshipCSV(
            "Missing required column Company_Name_Y "
            f"(accepted aliases: {', '.join(_Y_ALIASES)}). Found: {header}"
        )
    x_col = _find_column(normalized, _X_ALIASES)
    if x_col is None:
        raise InvalidRelationshipCSV(
            "Missing required column Company_Name_X "
            f"(accepted aliases: {', '.join(_X_ALIASES)}). Found: {header}"
        )
    url_col = _find_column(normalized, _URL_ALIASES)
    if url_col is None:
        raise InvalidRelationshipCSV(
            "Missing required column Input_URL. "
            f"Found: {header}"
        )
    city_col = _find_column(normalized, _CITY_ALIASES)
    country_col = _find_column(normalized, _COUNTRY_ALIASES)

    original_rows: list[dict[str, str]] = []
    blank_row_indices: list[int] = []
    pairs: list[dict] = []

    # One row in → one row out: no (X, Y) dedup. Each row becomes its own "pair"
    # carrying its single source row index (source_row_indices == [idx]).
    for idx, row in enumerate(reader):
        clean = {h: (row.get(h) or "").strip() for h in header}
        original_rows.append(clean)
        y_name = clean.get(y_col, "")
        x_name = clean.get(x_col, "")
        input_url = clean.get(url_col, "")
        missing = [name for name, value in (
            ("Input_URL", input_url),
            ("Company_Name_X", x_name),
            ("Company_Name_Y", y_name),
        ) if not value]
        if missing:
            raise InvalidRelationshipCSV(
                f"CSV row {idx + 2} missing required value(s): {', '.join(missing)}"
            )
        pairs.append({
            "pair_index": len(pairs) + 1,
            "x_name": x_name,
            "y_name": y_name,
            "input_url": input_url,
            "city": clean.get(city_col, "") if city_col else "",
            "country": clean.get(country_col, "") if country_col else "",
            "source_row_indices": [idx],
        })

    if not original_rows:
        raise InvalidRelationshipCSV("CSV has a header but no data rows.")

    return {
        "header": header,
        "original_rows": original_rows,
        "blank_row_indices": blank_row_indices,
        "pairs": pairs,
        # Which actual CSV headers matched each logical field (None when the
        # optional column is absent) — surfaced by the upload-preview endpoint.
        "columns_detected": {
            "company_name_y": y_col,
            "company_name_x": x_col,
            "input_url": url_col,
            "city": city_col,
            "country": country_col,
        },
    }
