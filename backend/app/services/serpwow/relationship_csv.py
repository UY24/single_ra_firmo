"""CSV parsing for the relationship pipeline (spec §2).

Input: OCR-results CSV with a required Company_Name_Y column, an expected
Company_Name_X column, optional city/country, and arbitrary passthrough
columns. Blank-Y rows are skipped (never searched). Searchable rows are
deduped into unique (X, Y) pairs; each pair remembers which original row
indices it fans back out to at reporting time.
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


def _pair_key(x_name: str, y_name: str) -> tuple[str, str]:
    collapse = lambda s: re.sub(r"\s+", " ", s.strip()).casefold()  # noqa: E731
    return (collapse(x_name), collapse(y_name))


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
    url_col = _find_column(normalized, _URL_ALIASES)
    city_col = _find_column(normalized, _CITY_ALIASES)
    country_col = _find_column(normalized, _COUNTRY_ALIASES)

    original_rows: list[dict[str, str]] = []
    blank_row_indices: list[int] = []
    pairs: list[dict] = []
    pair_by_key: dict[tuple[str, str], dict] = {}

    for idx, row in enumerate(reader):
        clean = {h: (row.get(h) or "").strip() for h in header}
        original_rows.append(clean)
        y_name = clean.get(y_col, "")
        if not y_name:
            blank_row_indices.append(idx)
            continue
        x_name = clean.get(x_col, "") if x_col else ""
        key = _pair_key(x_name, y_name)
        pair = pair_by_key.get(key)
        if pair is None:
            pair = {
                "pair_index": len(pairs) + 1,
                "x_name": x_name,
                "y_name": y_name,
                "input_url": clean.get(url_col, "") if url_col else "",
                "city": clean.get(city_col, "") if city_col else "",
                "country": clean.get(country_col, "") if country_col else "",
                "source_row_indices": [],
            }
            pair_by_key[key] = pair
            pairs.append(pair)
        else:
            # First non-blank value wins for the optional context fields.
            for col, field in ((url_col, "input_url"), (city_col, "city"),
                               (country_col, "country")):
                if col and not pair[field] and clean.get(col, ""):
                    pair[field] = clean[col]
        pair["source_row_indices"].append(idx)

    if not original_rows:
        raise InvalidRelationshipCSV("CSV has a header but no data rows.")

    return {
        "header": header,
        "original_rows": original_rows,
        "blank_row_indices": blank_row_indices,
        "pairs": pairs,
    }
