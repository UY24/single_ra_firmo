"""Self-contained Gemini Batch API helpers (native ``generativelanguage`` REST).

Used by :mod:`ai_mode_service` for the batch LLM-cleanup phase. Intentionally has
NO imports from ``app.py`` (avoids a circular import and keeps the SerpWow path
untouched). Stdlib-only (``urllib``) so it stays dependency-light.

Supports both batch input methods:
  * **inline** requests (small jobs, total payload < ~20 MB), and
  * **File API** upload (large jobs — one 2 GB file holds ~hundreds of thousands
    of requests, so no sharding is needed at our scale).

Results are mapped back to each request by a caller-supplied ``key`` (the
``"batch-NNN"`` scrape-batch index), never by position.

The two HTTP primitives are isolated in ``_http_post_json`` / ``_http_get_json``
(and ``_http_get_bytes``) so tests can monkeypatch them without network access.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable
from urllib.request import Request, urlopen

_API_ROOT = "https://generativelanguage.googleapis.com"
# Inline batch input must stay under 20 MB total; switch to the File API below
# this safe margin.
_INLINE_MAX_BYTES = 18 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Small env + HTTP primitives (monkeypatch points for tests)
# --------------------------------------------------------------------------- #
def _api_key() -> str:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not configured")
    return key


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except (ValueError, TypeError):
        return default


def _http_post_json(url: str, body: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
    req = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def _http_get_json(url: str, timeout: float = 60.0) -> dict[str, Any]:
    req = Request(url, headers={"Content-Type": "application/json"}, method="GET")
    with urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def _http_get_bytes(url: str, headers: dict[str, str], timeout: float = 300.0) -> bytes:
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=timeout) as response:
        return response.read()


# --------------------------------------------------------------------------- #
# messages -> Gemini request (mirrors GeminiClient.complete_json)
# --------------------------------------------------------------------------- #
def messages_to_gemini_request(messages: list[dict[str, str]], *, temperature: float = 0) -> dict[str, Any]:
    """Convert OpenAI-style ``messages`` into a Gemini ``GenerateContentRequest``.

    Mirrors ``app.services.ai_mode.llm_client.GeminiClient.complete_json`` exactly so a
    batched response is shaped identically to the synchronous path (JSON mode,
    same system instruction handling), and the existing parsers apply unchanged.
    """
    system_texts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        text = message.get("content", "")
        if role == "system":
            system_texts.append(text)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        contents.append({"role": gemini_role, "parts": [{"text": text}]})

    request: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {"responseMimeType": "application/json", "temperature": temperature},
    }
    if system_texts:
        request["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_texts)}]}
    return request


def build_jsonl(items: Iterable[tuple[str, dict[str, Any]]]) -> str:
    """Build batch-input JSONL text: one ``{"key","request"}`` line per item."""
    lines = [
        json.dumps({"key": key, "request": request}, ensure_ascii=False)
        for key, request in items
    ]
    return "\n".join(lines) + ("\n" if lines else "")


# --------------------------------------------------------------------------- #
# File API upload (resumable) — used when inline would exceed the cap
# --------------------------------------------------------------------------- #
def upload_jsonl_file(jsonl_text: str, display_name: str) -> str:
    """Upload ``jsonl_text`` via the File API (resumable) and return its name.

    Returns the file resource name (e.g. ``"files/abc123"``) to reference in a
    batch ``input_config.file_name``.
    """
    api_key = _api_key()
    data = jsonl_text.encode("utf-8")
    num_bytes = len(data)

    # Step 1 — start a resumable upload; the upload URL comes back in a header.
    start = Request(
        f"{_API_ROOT}/upload/v1beta/files",
        data=json.dumps({"file": {"display_name": display_name}}).encode("utf-8"),
        headers={
            "x-goog-api-key": api_key,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(num_bytes),
            "X-Goog-Upload-Header-Content-Type": "application/jsonl",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(start, timeout=60) as response:
        upload_url = response.headers.get("x-goog-upload-url") or response.headers.get(
            "X-Goog-Upload-URL"
        )
        response.read()
    if not upload_url:
        raise RuntimeError("Gemini File API did not return an upload URL")

    # Step 2 — upload the bytes and finalize in one request.
    upload = Request(
        upload_url,
        data=data,
        headers={
            "x-goog-api-key": api_key,
            "Content-Length": str(num_bytes),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
        method="POST",
    )
    with urlopen(upload, timeout=300) as response:
        raw = response.read().decode("utf-8")
    payload = json.loads(raw) if raw.strip() else {}
    file_obj = payload.get("file") if isinstance(payload, dict) else None
    name = ""
    if isinstance(file_obj, dict):
        name = str(file_obj.get("name") or "").strip()
    if not name and isinstance(payload, dict):
        name = str(payload.get("name") or "").strip()
    if not name:
        raise RuntimeError(f"Gemini File API upload returned no file name: {raw[:200]}")
    return name


# --------------------------------------------------------------------------- #
# Create / poll
# --------------------------------------------------------------------------- #
def create_batch(model: str, items: list[tuple[str, dict[str, Any]]], *, display_name: str) -> dict[str, Any]:
    """Create a batch job from ``items`` = ``[(key, request_dict), ...]``.

    Uses inline input when the payload is small, otherwise uploads a JSONL via the
    File API. Returns the create response (which carries the batch ``name``).
    """
    api_key = _api_key()
    endpoint = f"{_API_ROOT}/v1beta/models/{model}:batchGenerateContent?key={api_key}"
    jsonl_text = build_jsonl(items)
    if len(jsonl_text.encode("utf-8")) <= _INLINE_MAX_BYTES:
        requests_payload = [{"key": key, "request": request} for key, request in items]
        body = {
            "batch": {
                "display_name": display_name,
                "input_config": {"requests": {"requests": requests_payload}},
            }
        }
    else:
        file_name = upload_jsonl_file(jsonl_text, display_name)
        body = {
            "batch": {
                "display_name": display_name,
                "input_config": {"file_name": file_name},
            }
        }
    return _http_post_json(endpoint, body, timeout=120)


def batch_name_from_create(create_obj: dict[str, Any]) -> str:
    """Extract the batch/operation name from a create response."""
    if not isinstance(create_obj, dict):
        return ""
    return str(create_obj.get("name") or "").strip()


def get_batch(batch_name: str) -> dict[str, Any]:
    api_key = _api_key()
    return _http_get_json(f"{_API_ROOT}/v1beta/{batch_name}?key={api_key}", timeout=60)


def state_name(batch_obj: dict[str, Any]) -> str:
    """Return the job state name, tolerating old + LRO response shapes."""
    if not isinstance(batch_obj, dict):
        return ""
    state_obj = batch_obj.get("state")
    if isinstance(state_obj, dict):
        value = str(state_obj.get("name") or "").strip()
        if value:
            return value
    elif isinstance(state_obj, str) and state_obj.strip():
        return state_obj.strip()
    metadata = batch_obj.get("metadata")
    if isinstance(metadata, dict):
        metadata_state = metadata.get("state")
        if isinstance(metadata_state, str) and metadata_state.strip():
            return metadata_state.strip()
    if bool(batch_obj.get("done")):
        return "DONE"
    return ""


def is_terminal(state: str, done_flag: bool) -> bool:
    terminal = {
        "JOB_STATE_SUCCEEDED",
        "JOB_STATE_FAILED",
        "JOB_STATE_CANCELLED",
        "JOB_STATE_EXPIRED",
        "BATCH_STATE_SUCCEEDED",
        "BATCH_STATE_FAILED",
        "BATCH_STATE_CANCELLED",
        "BATCH_STATE_EXPIRED",
    }
    return state in terminal or bool(done_flag)


def is_success(state: str, done_flag: bool, batch_obj: dict[str, Any]) -> bool:
    if state in {"JOB_STATE_SUCCEEDED", "BATCH_STATE_SUCCEEDED"}:
        return True
    if not done_flag:
        return False
    return not bool(batch_obj.get("error"))


# --------------------------------------------------------------------------- #
# Result extraction (inline responses OR downloaded result file), by key
# --------------------------------------------------------------------------- #
def _text_from_response(resp_obj: Any) -> str:
    if not isinstance(resp_obj, dict):
        return ""
    candidates = resp_obj.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    first = candidates[0] if isinstance(candidates[0], dict) else {}
    content = first.get("content") if isinstance(first, dict) else {}
    parts = content.get("parts") if isinstance(content, dict) else []
    if not isinstance(parts, list):
        return ""
    return "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))


def parse_json_from_text(raw_text: str) -> dict[str, Any] | None:
    cleaned = (raw_text or "").strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1 :]
        cleaned = cleaned.rstrip("`").strip()
    try:
        parsed = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _response_key(item: dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    direct = item.get("key")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        meta_key = metadata.get("key")
        if isinstance(meta_key, str) and meta_key.strip():
            return meta_key.strip()
    request_obj = item.get("request")
    if isinstance(request_obj, dict):
        req_meta = request_obj.get("metadata")
        if isinstance(req_meta, dict):
            req_key = req_meta.get("key")
            if isinstance(req_key, str) and req_key.strip():
                return req_key.strip()
    return ""


def _inlined_responses(batch_obj: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(batch_obj, dict):
        return []
    dest = batch_obj.get("dest")
    if isinstance(dest, dict):
        for key in ("inlinedResponses", "inlined_responses"):
            value = dest.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    response_obj = batch_obj.get("response")
    if isinstance(response_obj, dict):
        nested = response_obj.get("inlinedResponses")
        if isinstance(nested, dict):
            value = nested.get("inlinedResponses")
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
    metadata = batch_obj.get("metadata")
    if isinstance(metadata, dict):
        output = metadata.get("output")
        if isinstance(output, dict):
            nested = output.get("inlinedResponses")
            if isinstance(nested, dict):
                value = nested.get("inlinedResponses")
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def _result_file_name(batch_obj: dict[str, Any]) -> str:
    if not isinstance(batch_obj, dict):
        return ""
    candidates: list[Any] = []
    response_obj = batch_obj.get("response")
    if isinstance(response_obj, dict):
        candidates.append(response_obj.get("responsesFile"))
        dest = response_obj.get("dest")
        if isinstance(dest, dict):
            candidates.append(dest.get("fileName") or dest.get("file_name"))
    dest = batch_obj.get("dest")
    if isinstance(dest, dict):
        candidates.append(dest.get("fileName") or dest.get("file_name"))
        candidates.append(dest.get("responsesFile"))
    metadata = batch_obj.get("metadata")
    if isinstance(metadata, dict):
        output = metadata.get("output")
        if isinstance(output, dict):
            candidates.append(output.get("responsesFile"))
            d = output.get("dest")
            if isinstance(d, dict):
                candidates.append(d.get("fileName") or d.get("file_name"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def download_result_file(file_name: str) -> str:
    api_key = _api_key()
    url = f"{_API_ROOT}/download/v1beta/{file_name}:download?alt=media"
    return _http_get_bytes(url, headers={"x-goog-api-key": api_key}).decode("utf-8")


def collect_results(batch_obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``[{key, text, usage, error}, ...]`` from a terminal SUCCESS batch.

    Handles both inline responses and a downloaded result file; both map by key.
    """
    results: list[dict[str, Any]] = []

    def _record(item: dict[str, Any]) -> dict[str, Any]:
        resp = item.get("response") if isinstance(item, dict) else None
        return {
            "key": _response_key(item),
            "text": _text_from_response(resp),
            "usage": (resp or {}).get("usageMetadata") if isinstance(resp, dict) else None,
            "error": item.get("error") if isinstance(item, dict) else None,
        }

    inlined = _inlined_responses(batch_obj)
    if inlined:
        return [_record(item) for item in inlined]

    file_name = _result_file_name(batch_obj)
    if file_name:
        raw = download_result_file(file_name)
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                results.append(_record(obj))
    return results


def calculate_gemini_batch_cost_usd(usage: dict[str, Any] | None) -> float:
    """Cost from a native Gemini ``usageMetadata`` object at batch (50%) rates."""
    if not usage:
        return 0.0
    input_tokens = int(usage.get("promptTokenCount", 0) or 0)
    output_tokens = int(usage.get("candidatesTokenCount", 0) or 0)
    input_usd_per_1m = _float_env(
        "GEMINI_BATCH_INPUT_USD_PER_1M_TOKENS", _float_env("GEMINI_INPUT_USD_PER_1M_TOKENS", 0.10)
    )
    output_usd_per_1m = _float_env(
        "GEMINI_BATCH_OUTPUT_USD_PER_1M_TOKENS", _float_env("GEMINI_OUTPUT_USD_PER_1M_TOKENS", 0.40)
    )
    cost = ((input_tokens / 1_000_000) * input_usd_per_1m) + (
        (output_tokens / 1_000_000) * output_usd_per_1m
    )
    return round(cost, 8)
