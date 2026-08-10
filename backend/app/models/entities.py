# backend/app/models/entities.py
"""The ONE canonical CSV input format, used by every pipeline (spec §3)."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

COMPANY_ALIASES = {"company_name", "company", "name", "entity_name", "entity",
                   "organization", "organisation", "legal_name"}
COUNTRY_ALIASES = {"country", "country_name", "nation"}
LOCAL_NAME_ALIASES = {"company_local_name", "local_name", "company_name_local", "name_local"}
ADDRESS_ALIASES = {"full_address", "address", "fulladdress", "input_full_address"}
FIRM_ID_ALIASES = {"firm_id", "firmid", "id"}
INDUSTRY_ALIASES = {"industry", "input_industry"}

_ALL_ALIASES = (COMPANY_ALIASES | COUNTRY_ALIASES | LOCAL_NAME_ALIASES
                | ADDRESS_ALIASES | FIRM_ID_ALIASES | INDUSTRY_ALIASES)

# Tokens that strongly suggest the first row is a header row (e.g. legacy
# Company Mode files) even though they map to no canonical column.
_HEADERISH_TOKENS = {"company_name_eng", "company_name_local", "country_code",
                     "isic", "sno", "s_no"}

REQUIRED_MESSAGE = (
    "CSV must contain a company name column (accepted: company_name, company, name, "
    "entity_name, entity, organization, organisation, legal_name) and a country column "
    "(accepted: country, country_name, nation). Optional: company_local_name/local_name, "
    "address/full_address, firm_id/id, industry. Headerless 2+ column files are parsed "
    "positionally (col 1 = company, col 2 = country)."
)


class InvalidCSVError(ValueError):
    pass


@dataclass
class Entity:
    company_name: str
    country: str
    sno: int
    company_local_name: str | None = None
    address: str | None = None
    firm_id: str | None = None
    industry: str | None = None


@dataclass
class ParsedCSV:
    entities: list[Entity]
    columns_detected: dict[str, str]   # canonical field -> original header
    warnings: list[str] = field(default_factory=list)
    positional: bool = False
    # Rows that PASSED validation, which is not len(entities) once sample_limit caps what
    # is retained. The S3-only pipelines need the count and nothing else: the rows
    # themselves go straight to S3 for the worker to stream back.
    total_rows: int = 0


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (header or "").strip().lower()).strip("_")


def _resolve_columns(fieldnames: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for original in fieldnames:
        n = _norm(original)
        for canon, aliases in (
            ("company_name", COMPANY_ALIASES), ("country", COUNTRY_ALIASES),
            ("company_local_name", LOCAL_NAME_ALIASES), ("address", ADDRESS_ALIASES),
            ("firm_id", FIRM_ID_ALIASES), ("industry", INDUSTRY_ALIASES),
        ):
            if n in aliases and canon not in mapping:
                mapping[canon] = original
    return mapping


def _looks_like_header_row(row: list[str]) -> bool:
    """True if any cell of the row resembles a known header name."""
    for cell in row:
        n = _norm(cell)
        if n and (n in _ALL_ALIASES or n in _HEADERISH_TOKENS):
            return True
    return False


def parse_entities_csv(raw: str | bytes,
                       sample_limit: int | None = None) -> ParsedCSV:
    """Validate every row; keep at most ``sample_limit`` of them as Entity objects.

    ``sample_limit=None`` (the default) keeps them all, which is what the AI Mode engine
    wants — it works from the entity list. The S3-only pipelines pass a small limit (or 0)
    because they need only the COUNT plus the raw bytes, which go straight to S3 for the
    worker to stream: materialising 500k Entity objects in the API process was ~150MB per
    upload of data no caller reads. ``total_rows`` always reflects every valid row.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig", errors="replace")
    header_row = next((r for r in csv.reader(io.StringIO(raw))
                       if any((c or "").strip() for c in r)), None)
    if header_row is None:
        raise InvalidCSVError("CSV is empty. " + REQUIRED_MESSAGE)

    mapping = _resolve_columns(header_row)
    warnings: list[str] = []
    entities: list[Entity] = []
    total = 0

    def _keep(entity: Entity) -> None:
        if sample_limit is None or len(entities) < sample_limit:
            entities.append(entity)

    if "company_name" in mapping and "country" in mapping:
        # Streamed, never materialised: DictReader over the text, one record at a time.
        reader = csv.DictReader(io.StringIO(raw))
        sno = 0
        for record in reader:
            name = (record.get(mapping["company_name"]) or "").strip()
            country = (record.get(mapping["country"]) or "").strip()
            if not name:
                continue
            sno += 1
            total += 1

            def opt(canon: str) -> str | None:
                header = mapping.get(canon)
                value = (record.get(header) or "").strip() if header else ""
                return value or None

            _keep(Entity(
                company_name=name, country=country, sno=sno,
                company_local_name=opt("company_local_name"), address=opt("address"),
                firm_id=opt("firm_id"), industry=opt("industry"),
            ))
        positional = False
    else:
        if _looks_like_header_row(header_row):
            # The file has a header row, but it is missing the required
            # company/country columns (e.g. legacy Company Mode exports).
            # Do NOT fall back to positional parsing, which would silently
            # treat header text as data.
            raise InvalidCSVError(
                "CSV header row is missing required columns. " + REQUIRED_MESSAGE)
        warnings.append("No recognized headers found; positional parsing used "
                        "(column 1 = company name, column 2 = country).")
        mapping = {"company_name": "<col 1>", "country": "<col 2>"}
        widest = 0
        sno = 0
        for row in csv.reader(io.StringIO(raw)):
            if not any((c or "").strip() for c in row):
                continue
            widest = max(widest, len(row))
            sno += 1
            if not (row and row[0].strip()):
                continue
            total += 1
            _keep(Entity(company_name=row[0].strip(),
                         country=(row[1].strip() if len(row) > 1 else ""), sno=sno))
        if widest < 2:
            raise InvalidCSVError("Unrecognized CSV format. " + REQUIRED_MESSAGE)
        positional = True

    if not total:
        raise InvalidCSVError("No valid rows found. " + REQUIRED_MESSAGE)
    return ParsedCSV(entities=entities, total_rows=total, columns_detected=mapping,
                     warnings=warnings, positional=positional)


def format_entities_for_prompt(entities: list[Entity]) -> str:
    """Builds the {entities} block. Optional fields appear only when present (spec §3)."""
    lines = []
    for e in entities:
        line = e.company_name
        if e.company_local_name:
            line += f" (local: {e.company_local_name})"
        line += f" — {e.country}"
        if e.address:
            line += f" — {e.address}"
        if e.industry:
            line += f" — industry: {e.industry}"
        lines.append(f"{e.sno}. {line}")
    return "\n".join(lines)
