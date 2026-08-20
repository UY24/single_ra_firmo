# backend/app/services/serpwow/s3_run_store.py
"""S3-only run store, shared by the relationship and gmaps pipelines.

There is NO local disk and NO state.json. Object presence IS the state:
  raw/<shard>/row_NNNNNN.json      -> scrape.do's response body, EXACTLY as sent
  rows/<shard>/row_NNNNNN.json     -> the row's computed result (gmaps)
  errors/<shard>/row_NNNNNN.json   -> the row died after every retry, and why
  cleaned/<shard>/row_NNNNNN.json  -> the row has a Gemini verdict (relationship)
  batches/<first>-<last>.json      -> a Gemini shard is IN FLIGHT for those rows

Every function takes an explicit run ``prefix``, so nothing here is pipeline-specific
except which SEGMENT a new run's prefix is built under and which pointer namespace it is
indexed in — both passed in, both defaulting to nothing.

``raw/`` holds NOTHING but the provider's own bytes — no wrapper, no fields of ours, not
even re-serialised JSON. There is no sidecar object either: everything a sidecar would
have carried is already somewhere else. The query and locale come back inside the
response's own ``search_parameters``; the row index IS the filename; the row's CSV fields
are in input.csv, which both later phases stream anyway; credits are 10 per successful
call; and the run's request total is a counter in status.json.

A row is DONE when its raw object exists, or its error object does.

NNNNNN counts from 1 (input.csv's first data row is row_000001); <shard> is that row's
0-based index // SHARD_SIZE, so folder 0 holds rows 1-1000, folder 1 holds 1001-2000.
The sharding exists because 500k objects under one prefix make every LIST slow and
hot-key a single S3 partition.

That makes the EC2 instance disposable: nothing to size, nothing to rehydrate, and a
replaced box just re-drives. It is the model gmaps already uses for raw responses.

Writes here are STRICT — put_object RAISES. ai_mode/s3_sync.mirror_file_to_s3 is
deliberately best-effort because AI Mode keeps a local copy as the truth; with S3 as the
ONLY copy, a swallowed PUT failure silently loses a row's work.
"""
from __future__ import annotations

import calendar
import csv
import io
import json
import time
from typing import Any, Iterator, Optional

from app.core import s3
from app.services.common.env import get_float_env
from app.services.common.text import slugify_company

PIPELINE_SEGMENT = "relationship"
# One prefix per 1000 rows: 500 prefixes at 500k, so no LIST page is huge and writes
# spread across prefixes instead of hot-keying one.
SHARD_SIZE = 1000


def _client():
    return s3.get_s3_client()


def _bucket() -> str:
    return s3.bucket_name()


# ---------------------------------------------------------------- key layout

def run_prefix(company_name: str, run_id: str,
               segment: str = PIPELINE_SEGMENT) -> str:
    return f"{slugify_company(company_name)}/{segment}/{run_id}"


def _shard(idx: int) -> int:
    return int(idx) // SHARD_SIZE


def _row_name(idx: int) -> str:
    """Filename stem for a 0-based row index, numbered from 1.

    The index stays 0-based everywhere in code (it is the CSV's enumerate() position and
    the Gemini batch key); only the FILENAME is 1-based, so row_000001.json is the first
    data row of input.csv and matches what a spreadsheet shows. _idx_from_key undoes it.
    """
    return f"row_{int(idx) + 1:06d}"


def raw_key(prefix: str, idx: int) -> str:
    """The provider's response body, verbatim. Nothing of ours goes in here."""
    return f"{prefix}/raw/{_shard(idx)}/{_row_name(idx)}.json"


def row_key(prefix: str, idx: int) -> str:
    """A row's COMPUTED result, beside the provider bytes it was derived from.

    gmaps needs this and relationship does not: a gmaps row's attempt accounting
    (which of its retries recovered, which were real errors, whether Google simply has
    no listing) cannot be recovered from the response body, and the UI's error breakdown
    is built from exactly those numbers. relationship re-derives its row result from
    raw/ + input.csv instead, so it writes no object here.
    """
    return f"{prefix}/rows/{_shard(idx)}/{_row_name(idx)}.json"


def error_key(prefix: str, idx: int) -> str:
    """A row that died after every retry. NOT under raw/ — raw/ is the provider's bytes,
    and this is our record of a call that never produced any."""
    return f"{prefix}/errors/{_shard(idx)}/{_row_name(idx)}.json"


def pending_llm_key(prefix: str, idx: int) -> str:
    """Marker: this row's LLM work was DEFERRED to a batch job.

    Written by firmographics' scrape phase only when batching is on. Its whole purpose is
    to make phase 2's pending set ONE paginated LIST instead of one GET per row: without it
    the phase had to open every rows/ object to ask "was this one deferred?", which is
    500k GETs at scale — in a phase that does nothing at all in inline mode.
    """
    return f"{prefix}/pending_llm/{_shard(idx)}/{_row_name(idx)}"


def list_pending_llm_rows(prefix: str) -> set[int]:
    """Row indices whose LLM work was deferred to a batch. Empty in inline mode."""
    return list_done_rows(prefix, "pending_llm")


def cleaned_key(prefix: str, idx: int) -> str:
    return f"{prefix}/cleaned/{_shard(idx)}/{_row_name(idx)}.json"


def batch_record_key(prefix: str, indices: list[int]) -> str:
    """Where one in-flight Gemini shard's job name is remembered.

    Derived from the shard's first and last row index so the submitter and the code that
    clears it can both compute it without passing it around. Indices within a run are
    disjoint, so two live shards can never collide.
    """
    first, last = (int(indices[0]), int(indices[-1])) if indices else (0, 0)
    return f"{prefix}/batches/{first + 1:06d}-{last + 1:06d}.json"


def list_batch_records(prefix: str) -> list[dict[str, Any]]:
    """Every remembered in-flight shard, each with the key it lives at so the caller can
    delete it once its verdicts are durable."""
    out: list[dict[str, Any]] = []
    for key in iter_keys(f"{prefix}/batches/"):
        record = get_object(key)
        if record and record.get("name"):
            out.append({**record, "record_key": key})
    return out


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
    """Write raw bytes (input.csv, report.json — anything small enough to hold in
    memory whole). RAISES on failure. See put_fileobj for anything that scales with
    row count (the output CSVs, run.log)."""
    _client().put_object(Bucket=_bucket(), Key=key, Body=data, ContentType=content_type)


def put_fileobj(key: str, fileobj: Any, content_type: str = "text/csv") -> None:
    """Upload a file-like object via boto3's managed transfer (multipart handled
    internally). Lets a caller assemble one output artifact in a spooled temp file
    instead of an in-memory buffer, so a 500k-row CSV/log costs O(chunk) memory, not
    O(file). Same strict contract as put_bytes: RAISES on failure."""
    fileobj.seek(0)
    _client().upload_fileobj(fileobj, _bucket(), key, ExtraArgs={"ContentType": content_type})


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


DELETE_BATCH_SIZE = 1000   # S3's per-request maximum for delete_objects


def delete_objects(keys: list[str]) -> int:
    """Delete many keys and return how many S3 CONFIRMED gone.

    Not a loop over delete_object: that is one HTTPS round-trip per key, so clearing 50k
    error markers took 20+ minutes inside a single web request and timed the client out.
    delete_objects takes 1000 keys per call, turning 50k deletes into 50 calls.

    The count is confirmed, not assumed — a swallowed failure would be reported to the
    user as a retried row that then never gets rescraped, because its error marker is
    still there and marks it done.
    """
    deleted = 0
    for start in range(0, len(keys), DELETE_BATCH_SIZE):
        chunk = keys[start:start + DELETE_BATCH_SIZE]
        try:
            response = _client().delete_objects(
                Bucket=_bucket(),
                Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": False},
            )
        except Exception:
            continue   # whole chunk unconfirmed; the next re-drive can retry it
        deleted += len(response.get("Deleted") or [])
    return deleted


def iter_keys(prefix: str) -> Iterator[str]:
    paginator = _client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            yield obj["Key"]


# Credits per successful (HTTP 200) scrape.do call. Duplicated from
# scrapedo_maps_client.CREDITS_PER_CALL rather than imported, to keep this store free of
# any provider-client dependency.
CREDITS_PER_CALL = 10


def write_row(prefix: str, idx: int, envelope: dict[str, Any]) -> None:
    """Persist one scraped row: ONE object, holding the provider's body and nothing else.

    ``response_text`` is written as bytes exactly as scrape.do sent them — no re-encoding,
    no re-serialisation, no wrapper. This object is read by hand to judge the search
    prompt, so anything of ours in it is in the way.

    Nothing of ours is lost by that, because none of it was ours to begin with: the query
    and locale come back inside ``search_parameters``, the row index is the filename, the
    CSV fields are in input.csv, credits are 10 per successful call, and the run's request
    total already lives in status.json's counters.

    A row that died after every retry has no body worth keeping, so it gets an object
    under ``errors/`` instead — that one IS ours (attempts, message, category), which is
    how a re-drive knows not to retry it free forever and "Rerun failed" can find it.
    """
    body = envelope.get("response_text")
    if not isinstance(body, str) and isinstance(envelope.get("response"), dict):
        # No wire text captured (an injected/synthesised envelope): serialise the parsed
        # body rather than writing an empty object. Still only the provider's fields.
        body = json.dumps(envelope["response"], ensure_ascii=False)
    if envelope.get("error"):
        put_object(error_key(prefix, idx),
                   {k: v for k, v in envelope.items()
                    if k not in ("response", "response_text")})
        return
    put_bytes(raw_key(prefix, idx),
              (body if isinstance(body, str) else "").encode("utf-8"),
              content_type="application/json")


def read_row(prefix: str, idx: int) -> Optional[dict[str, Any]]:
    """One row's envelope: the stored provider body plus the counts implied by it.

    A row with a raw object was, by definition, one billed HTTP 200 — so credits and the
    success count are constants, not something worth a second object per row. The one
    figure that genuinely cannot be recovered is how many ATTEMPTS the row took before
    that 200; the run-level total is in status.json's ``requests`` counter instead.
    """
    response = get_object(raw_key(prefix, idx))
    if response is not None:
        return {"response": response, "request_count": 1, "successful_requests": 1,
                "failed_requests": 0, "credits": CREDITS_PER_CALL,
                "error": None, "error_category": None}
    return get_object(error_key(prefix, idx))


# ---------------------------------------------------------------- resume

def _idx_from_key(key: str) -> Optional[int]:
    """0-based row index from a 1-based filename (see _row_name). Inverse of raw_key."""
    name = key.rsplit("/", 1)[-1]
    if not name.startswith("row_"):
        return None
    digits = name[4:10]
    if not digits.isdigit():
        return None
    # row_000000 can only come from a pre-1-based run; clamp rather than return -1,
    # which would silently mark a phantom row done.
    return max(0, int(digits) - 1)


def list_done_rows(prefix: str, done_prefix: str = "raw") -> set[int]:
    """Row indices that are finished: they have a ``done_prefix`` object, or an error marker.

    Paginated LISTs — a few seconds for 500k rows — instead of 500k HEADs. Every later
    skip check is an in-memory set lookup.

    ``done_prefix`` is what "finished" MEANS for the pipeline: relationship's raw/ object
    is written last so it is the completion marker, while gmaps writes raw/ first and its
    rows/ result second — so a gmaps row is only done once rows/ exists, and a crash
    between the two writes just re-scrapes it.
    """
    done: set[int] = set()
    for key in iter_keys(f"{prefix}/{done_prefix}/"):
        idx = _idx_from_key(key)
        if idx is not None:
            done.add(idx)
    for key in iter_keys(f"{prefix}/errors/"):
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

def _pointer_key(run_id: str, segment: str = PIPELINE_SEGMENT) -> str:
    """One index object per run, namespaced per pipeline.

    ``_relationship_runs/`` is what the existing relationship runs already use, so
    deriving the namespace from the segment keeps them findable with no migration and
    gives gmaps ``_gmaps_runs/`` for free.
    """
    return f"_{segment}_runs/{run_id}.json"


def write_run_pointer(run_id: str, prefix: str, company_name: str,
                      run_db_id: Optional[str] = None,
                      segment: str = PIPELINE_SEGMENT) -> None:
    """run_id -> prefix, so the API can find a run without scanning the bucket.

    Also carries the Supabase ``run_db_id``: a relationship run has no state dict, so
    this pointer is where phase 3 finds the row it must update at terminal status.
    """
    put_object(_pointer_key(run_id, segment),
               {"run_id": run_id, "prefix": prefix, "company_name": company_name,
                "run_db_id": run_db_id})


def read_run_pointer(run_id: str,
                     segment: str = PIPELINE_SEGMENT) -> Optional[dict[str, Any]]:
    return get_object(_pointer_key(run_id, segment))


def list_run_pointers(segment: str = PIPELINE_SEGMENT) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in iter_keys(f"_{segment}_runs/"):
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
        if "row_index" in clean:
            # A real input column literally named row_index would otherwise be
            # silently clobbered by the injected index below with no trace of the
            # original value — preserve it under a distinct key first.
            clean["row_index__input"] = clean["row_index"]
        clean["row_index"] = idx
        yield clean


def read_input_header(prefix: str) -> list[str]:
    """Just the header row. Streams the object body (same pattern as
    iter_input_rows) rather than reading the whole file with get_bytes: a 500k-row
    CSV can be well over 100MB and this only needs the first line. Blank cells (a
    trailing comma in the source CSV) are filtered, matching the old writer."""
    body = _client().get_object(Bucket=_bucket(), Key=input_key(prefix))["Body"]
    try:
        text = io.TextIOWrapper(body, encoding="utf-8-sig", newline="")
        row = next(csv.reader(text), [])
    finally:
        body.close()
    return [h for h in row if h]


# ---------------------------------------------------------------- stop marker

def request_stop(prefix: str) -> None:
    put_bytes(stop_key(prefix), b"1", content_type="text/plain")


def clear_stop(prefix: str) -> None:
    delete_object(stop_key(prefix))


def stop_requested(prefix: str) -> bool:
    return get_bytes(stop_key(prefix)) is not None


# ---------------------------------------------------------------- counters

def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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

    # task_errors: row/shard tasks that raised (see relationship_runner._drain). Not a
    # row count — one verdict-shard error can strand thousands of rows — but it is the
    # only durable trace that something failed outside the per-row error markers.
    # scrape_seconds / llm_seconds are cumulative per-phase wall clock, so the run can
    # answer "how long did scrape.do take vs the LLM" without a second store. Integers
    # like every other field, so status.json stays ~2KB at any run size.
    # drive_attempts: how many times drive_run has died on this run. Lets the re-drive
    # scan retry a run whose phase is "failed" a bounded number of times instead of
    # treating one transient error as permanent — see relationship_runner.
    # rows_no_listing: gmaps rows Google has no Maps listing for. Unbilled and not an
    # error, but it is what explains a bill under rows x 10 credits, so the live status
    # has to carry it — write_outputs recomputes it from the rows themselves.
    _FIELDS = ("rows_total", "rows_scraped", "rows_failed", "rows_billed_empty",
               "rows_no_listing", "rows_cleaned", "requests", "credits", "task_errors",
               "scrape_seconds", "llm_seconds", "drive_attempts")

    def __init__(self, prefix: str, rows_total: int = 0, phase: str = "queued",
                 created_at: str | None = None) -> None:
        self.prefix = prefix
        self.values: dict[str, int] = {f: 0 for f in self._FIELDS}
        self.values["rows_total"] = int(rows_total)
        self.phase = phase
        # Set once, at upload, and carried forward by every later drive — it is what makes
        # total wall clock reportable. updated_at alone cannot give it.
        self.created_at = created_at or _utc_now()
        self._last_flush = 0.0

    def bump(self, **deltas: int) -> None:
        for key, delta in deltas.items():
            # Loud, not silent: an unlisted key used to be dropped here, which is how
            # gmaps' rows_no_listing counted nothing for a whole pipeline without anyone
            # noticing. Every call site passes a literal, so this can only fire on a typo.
            if key not in self.values:
                raise KeyError(f"unknown counter {key!r}")
            self.values[key] += int(delta)

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def snapshot(self) -> dict[str, Any]:
        return {**self.values, "phase": self.phase,
                "created_at": self.created_at, "updated_at": _utc_now()}

    def elapsed_seconds(self) -> Optional[int]:
        """Wall clock from created_at to now, or None if created_at is unparseable.

        Survives a worker restart because created_at lives in status.json, not memory —
        which is the whole reason total run time is reportable at all here.
        """
        try:
            started = calendar.timegm(
                time.strptime(str(self.created_at), "%Y-%m-%dT%H:%M:%SZ"))
        except (TypeError, ValueError):
            return None
        return max(0, int(time.time() - started))

    def flush(self, force: bool = False) -> None:
        interval = max(0.0, get_float_env("RELATIONSHIP_STATUS_FLUSH_SEC", 2.0))
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
