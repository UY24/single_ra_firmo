"""CSV parsing for the relationship pipeline.

Input: OCR-results CSV with required Input_URL, Company_Name_X and Company_Name_Y
values on every row, plus arbitrary passthrough columns. Those three are the ONLY
fields this pipeline gets — there is no location, because the OCR'd portfolio page
does not carry one. Every row is processed independently: one row in, one row out.

Every row is VALIDATED, but only the first ``sample_limit`` are kept in memory. This
runs in the API process, so materialising 500k dicts (twice, as it used to — once for
the originals and once for the parsed rows) meant ~2GB per upload for data neither
caller wants: the preview shows a handful of rows, and the upload path only needs the
row COUNT plus the raw bytes, which go straight to S3 for the worker to stream.
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


def _find_column(normalized: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return None


SAMPLE_LIMIT = 10


def parse_relationship_csv(raw: bytes, sample_limit: int = SAMPLE_LIMIT) -> dict:
    """Validate every row; return the count plus the first ``sample_limit`` rows."""
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
    rows: list[dict] = []
    total_rows = 0

    for idx, row in enumerate(reader):
        total_rows += 1
        y_name = (row.get(y_col) or "").strip()
        x_name = (row.get(x_col) or "").strip()
        input_url = (row.get(url_col) or "").strip()
        missing = [name for name, value in (
            ("Input_URL", input_url),
            ("Company_Name_X", x_name),
            ("Company_Name_Y", y_name),
        ) if not value]
        if missing:
            raise InvalidRelationshipCSV(
                f"CSV row {idx + 2} missing required value(s): {', '.join(missing)}"
            )
        # Validation above runs on EVERY row — only retention is capped.
        if len(rows) < max(0, sample_limit):
            rows.append({
                "row_index": idx,
                "x_name": x_name,
                "y_name": y_name,
                "input_url": input_url,
            })

    if not total_rows:
        raise InvalidRelationshipCSV("CSV has a header but no data rows.")

    return {
        "header": header,
        "total_rows": total_rows,
        # The first `sample_limit` rows only — never the whole file. Nothing downstream
        # reads rows from here: the worker streams input.csv from S3.
        "rows": rows,
        # Which actual CSV header matched each logical field — surfaced by the
        # upload-preview endpoint.
        "columns_detected": {
            "company_name_y": y_col,
            "company_name_x": x_col,
            "input_url": url_col,
        },
    }
