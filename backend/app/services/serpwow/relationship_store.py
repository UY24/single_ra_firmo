# backend/app/services/serpwow/relationship_store.py
"""S3-only run store for the relationship pipeline.

There is NO local disk and NO state.json. Object presence IS the state:
  raw/<shard>/row_NNNNNN.json        -> the row is scraped (done)
  raw/<shard>/row_NNNNNN.error.json  -> the row died after every retry
  cleaned/<shard>/row_NNNNNN.json    -> the row has a Gemini verdict

That makes the EC2 instance disposable: nothing to size, nothing to rehydrate, and a
replaced box just re-drives. It is the model gmaps already uses for raw responses.

Writes here are STRICT — put_object RAISES. ai_mode/s3_sync.mirror_file_to_s3 is
deliberately best-effort because AI Mode keeps a local copy as the truth; with S3 as the
ONLY copy, a swallowed PUT failure silently loses a row's work.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from typing import Any, Iterator, Optional

from app.core import s3
from app.services.common.text import slugify_company

PIPELINE_SEGMENT = "relationship"
# One prefix per 1000 rows: 500 prefixes at 500k, so no LIST page is huge and writes
# spread across prefixes instead of hot-keying one.
SHARD_SIZE = 1000
_POINTER_PREFIX = "_relationship_runs"


def _client():
    return s3.get_s3_client()


def _bucket() -> str:
    return s3.bucket_name()


# ---------------------------------------------------------------- key layout

def run_prefix(company_name: str, run_id: str) -> str:
    return f"{slugify_company(company_name)}/{PIPELINE_SEGMENT}/{run_id}"


def _shard(idx: int) -> int:
    return int(idx) // SHARD_SIZE


def raw_key(prefix: str, idx: int) -> str:
    return f"{prefix}/raw/{_shard(idx)}/row_{int(idx):06d}.json"


def error_key(prefix: str, idx: int) -> str:
    return f"{prefix}/raw/{_shard(idx)}/row_{int(idx):06d}.error.json"


def cleaned_key(prefix: str, idx: int) -> str:
    return f"{prefix}/cleaned/{_shard(idx)}/row_{int(idx):06d}.json"


def status_key(prefix: str) -> str:
    return f"{prefix}/status.json"


def input_key(prefix: str) -> str:
    return f"{prefix}/input.csv"


def stop_key(prefix: str) -> str:
    return f"{prefix}/stop_requested"


# ---------------------------------------------------------------- object I/O

def put_object(key: str, payload: dict[str, Any]) -> None:
    """Write one JSON object. RAISES on failure — see the module docstring."""
    _client().put_object(
        Bucket=_bucket(), Key=key,
        Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )


def put_bytes(key: str, data: bytes, content_type: str = "text/csv") -> None:
    """Write raw bytes (input.csv, the output CSVs, run.log). RAISES on failure."""
    _client().put_object(Bucket=_bucket(), Key=key, Body=data, ContentType=content_type)


def get_object(key: str) -> Optional[dict[str, Any]]:
    """Parsed JSON object, or None when the key is absent or unparseable.

    Unparseable is treated as absent on purpose: a truncated object means the row is not
    really done, so the next drive should redo it.
    """
    try:
        body = _client().get_object(Bucket=_bucket(), Key=key)["Body"].read()
    except Exception:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def get_bytes(key: str) -> Optional[bytes]:
    try:
        return _client().get_object(Bucket=_bucket(), Key=key)["Body"].read()
    except Exception:
        return None


def delete_object(key: str) -> None:
    try:
        _client().delete_object(Bucket=_bucket(), Key=key)
    except Exception:
        pass


def iter_keys(prefix: str) -> Iterator[str]:
    paginator = _client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            yield obj["Key"]


# ---------------------------------------------------------------- resume

def _idx_from_key(key: str) -> Optional[int]:
    name = key.rsplit("/", 1)[-1]
    if not name.startswith("row_"):
        return None
    digits = name[4:10]
    return int(digits) if digits.isdigit() else None


def list_done_rows(prefix: str) -> set[int]:
    """Row indices that already have a raw OR error object.

    ONE paginated LIST — 500 requests at 1000 keys/page for 500k rows, a few seconds —
    instead of 500k HEADs. Every later skip check is an in-memory set lookup.
    """
    done: set[int] = set()
    for key in iter_keys(f"{prefix}/raw/"):
        idx = _idx_from_key(key)
        if idx is not None:
            done.add(idx)
    return done


def list_cleaned_rows(prefix: str) -> set[int]:
    done: set[int] = set()
    for key in iter_keys(f"{prefix}/cleaned/"):
        idx = _idx_from_key(key)
        if idx is not None:
            done.add(idx)
    return done


# ---------------------------------------------------------------- run pointer

def _pointer_key(run_id: str) -> str:
    return f"{_POINTER_PREFIX}/{run_id}.json"


def write_run_pointer(run_id: str, prefix: str, company_name: str,
                      run_db_id: Optional[str] = None) -> None:
    """run_id -> prefix, so the API can find a run without scanning the bucket.

    Also carries the Supabase ``run_db_id``: a relationship run has no state dict, so
    this pointer is where phase 3 finds the row it must update at terminal status.
    """
    put_object(_pointer_key(run_id),
               {"run_id": run_id, "prefix": prefix, "company_name": company_name,
                "run_db_id": run_db_id})


def read_run_pointer(run_id: str) -> Optional[dict[str, Any]]:
    return get_object(_pointer_key(run_id))


def list_run_pointers() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in iter_keys(f"{_POINTER_PREFIX}/"):
        ptr = get_object(key)
        if ptr:
            out.append(ptr)
    return out


# ---------------------------------------------------------------- input CSV

def iter_input_rows(prefix: str) -> Iterator[dict[str, str]]:
    """Stream input.csv from S3, one dict per data row, with a 0-based row_index.

    Reads the object body as a stream so a 500k-row CSV is never fully materialised as
    a list of dicts.
    """
    body = _client().get_object(Bucket=_bucket(), Key=input_key(prefix))["Body"]
    text = io.TextIOWrapper(body, encoding="utf-8-sig", newline="")
    for idx, row in enumerate(csv.DictReader(text)):
        clean = {k: (v or "").strip() for k, v in row.items() if k is not None}
        clean["row_index"] = idx
        yield clean


def read_input_header(prefix: str) -> list[str]:
    body = get_bytes(input_key(prefix)) or b""
    text = body.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    return next(reader, [])


# ---------------------------------------------------------------- stop marker

def request_stop(prefix: str) -> None:
    put_bytes(stop_key(prefix), b"1", content_type="text/plain")


def clear_stop(prefix: str) -> None:
    delete_object(stop_key(prefix))


def stop_requested(prefix: str) -> bool:
    return get_bytes(stop_key(prefix)) is not None


# ---------------------------------------------------------------- counters

def read_status(prefix: str) -> Optional[dict[str, Any]]:
    return get_object(status_key(prefix))


class Counters:
    """O(1) run counters, PUT on a timer. A CACHE, never the truth.

    status.json is ~2KB whether the run is 100 rows or 500k — it holds integers only and
    never a row. The phase barrier re-LISTs before advancing, and a re-drive rebuilds
    these from what is actually in S3, so a stale or lost status.json costs nothing.

    Single writer: the API writes it once at upload, the worker owns it from the first
    scrape onward.
    """

    _FIELDS = ("rows_total", "rows_scraped", "rows_failed", "rows_billed_empty",
               "rows_cleaned", "requests", "credits")

    def __init__(self, prefix: str, rows_total: int = 0, phase: str = "queued") -> None:
        self.prefix = prefix
        self.values: dict[str, int] = {f: 0 for f in self._FIELDS}
        self.values["rows_total"] = int(rows_total)
        self.phase = phase
        self._last_flush = 0.0

    def bump(self, **deltas: int) -> None:
        for key, delta in deltas.items():
            if key in self.values:
                self.values[key] += int(delta)

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def snapshot(self) -> dict[str, Any]:
        return {**self.values, "phase": self.phase,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def flush(self, force: bool = False) -> None:
        interval = _flush_interval()
        now = time.monotonic()
        if not force and (now - self._last_flush) < interval:
            return
        self._last_flush = now
        try:
            put_object(status_key(self.prefix), self.snapshot())
        except Exception:
            # A missed counter flush is cosmetic — the objects are the truth. Never let
            # it kill a run that is otherwise progressing fine.
            pass


def _flush_interval() -> float:
    raw = os.getenv("RELATIONSHIP_STATUS_FLUSH_SEC", "").strip()
    try:
        return max(0.0, float(raw)) if raw else 2.0
    except ValueError:
        return 2.0
