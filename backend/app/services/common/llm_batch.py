# backend/app/services/common/llm_batch.py
"""One resolver for every Gemini Batch setting, shared by all four pipelines.

All four (``ai_bulk``/``ai_deep``, ``gsearch``, ``firmographics``, ``relationship``) already
call ONE driver — ``ai_mode/gemini_batch.py``. Only the config around it had drifted: the
shard size lived under two key names with the same default, the model resolved differently
per pipeline, and there were three toggles plus one pipeline with none.

Lives in ``common/`` because it must be importable from both ``serpwow/`` and ``ai_mode/``
without a cycle, alongside the other cross-provider helpers (``text``, ``env``,
``provider_limits``).

**ONE toggle: ``LLM_BATCH``.** The per-pipeline overrides (``AI_MODE_LLM_BATCH``,
``GSEARCH_LLM_BATCH``, ``FIRMOGRAPHICS_LLM_BATCH``) were deleted 2026-08-20. They bought
granularity nobody used and cost a second place to look when a run came out in the wrong
mode: the global only appeared to work because all three happened to be blank, and one
non-blank value silently exempted that pipeline from it.

**Blank counts as unset.** ``env.get_bool_env`` falls back only when a variable is ABSENT,
while ``.env.example`` ships every key as ``NAME=`` — so a blank line would otherwise read
as an explicit "off". That check is made once, here, rather than at each call site.
"""
from __future__ import annotations

import os

from app.services.common.env import get_int_env

# Google's published Batch defaults. Not per-deployment opinions.
DEFAULT_MODEL = "gemini-2.5-flash-lite"
DEFAULT_SHARD_SIZE = 5000
DEFAULT_MAX_INFLIGHT = 5
# 48h == Gemini's own job expiry. A Batch job runs and bills on Google's side whether or not
# we are still polling, so a shorter deadline abandons work already paid for. This replaced a
# 30-minute default that could not outlast a real job.
DEFAULT_TIMEOUT_SEC = 172800
DEFAULT_POLL_SEC = 15

# Pipelines that genuinely have a choice, i.e. the ones LLM_BATCH moves. A pipeline absent
# here has no toggle, and inventing one would mean a setting that cannot be honoured:
#   relationship -- its Gemini call IS the verdict; there is no inline path to switch to.
#   gmaps        -- no LLM at all since it moved to the S3-only runner.
#
# Spelled out rather than derived from _SHARED_ROW_BATCH below: AI Mode is toggleable but
# does NOT run through engine's shared row-batch driver, and conflating those two sets is
# the exact bug this module exists to prevent.
_TOGGLEABLE = {"ai_bulk", "ai_deep", "gsearch", "firmographics"}

# Pipelines whose LLM work is ALWAYS a batch job, toggle or no toggle.
_ALWAYS_BATCH = {"relationship"}

_TRUE = {"1", "true", "yes", "on"}


def _flag(name: str) -> bool | None:
    """Tri-state read: True / False / None for absent-or-blank.

    The None keeps a blank ``LLM_BATCH=`` line (what ``.env.example`` ships) from reading as
    an explicit "off" that a caller might treat differently from "not configured".
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in _TRUE


# Pipelines whose batch work runs through engine's SHARED row-batch engine (one Gemini job
# per chunk of rows, driven from persist_upload_state). relationship is absent even though it
# batches: it owns a separate driver in relationship_runner, so routing it here would seed a
# second, duplicate job. Keeping this apart from batch_enabled is the difference between
# "is this batched?" and "is it batched BY THIS ENGINE?" -- conflating them made engine's gate
# claim relationship.
_SHARED_ROW_BATCH = {"gsearch", "firmographics"}


def uses_shared_row_batch(pipeline: str) -> bool:
    """True when ``engine``'s chunked row-batch driver should handle this pipeline.

    This is the gate for ``maybe_start_gemini_batch_for_upload`` and friends — NOT a general
    "is batching on" question. See ``batch_enabled`` for that.
    """
    return str(pipeline or "") in _SHARED_ROW_BATCH and batch_enabled(pipeline)


def batch_enabled(pipeline: str) -> bool:
    """True when this pipeline's Gemini work should run as a Batch job instead of inline.

    ``LLM_BATCH`` is the only knob, and it moves every pipeline that has a choice at once
    (2026-08-20). Per-pipeline granularity is gone on purpose — see the module docstring.

    Note for AI Mode: turning this on ALSO forces the Gemini provider over
    ``AI_MODE_LLM_PROVIDER``, because batch cleanup is Gemini-only (see
    ``ai_mode_service.build_ai_mode_llm_config``). That side effect used to ride the
    AI-Mode-specific key and now rides the global; this function still only answers
    batch-vs-inline.
    """
    pipe = str(pipeline or "")
    if pipe in _ALWAYS_BATCH:
        return True
    if pipe not in _TOGGLEABLE:
        return False
    return _flag("LLM_BATCH") or False


def shard_failure(gb, obj: dict) -> str | None:
    """The terminal state of a shard Google produced NOTHING for, else None.

    ``is_terminal()`` is true for SUCCEEDED, FAILED, CANCELLED and EXPIRED alike — it
    answers "stop polling", not "there are results". A caller that checks only terminality
    treats a dead job as an answered one, and both runners then write an empty ``cleaned/``
    object for every key in the shard. That object IS the row's done-marker, so the rows are
    permanently blank: no retry, no error, and the scrape.do spend behind them wasted.

    ``gb`` is ``ai_mode.gemini_batch``, passed in rather than imported: it is imported lazily
    at the call sites to keep this module free of the ai_mode dependency.
    """
    state = gb.state_name(obj)
    if gb.is_success(state, bool(obj.get("done")), obj):
        return None
    return str(state or "unknown")


def batch_model() -> str:
    """The model for Batch jobs: ``GEMINI_BATCH_MODEL`` -> ``GEMINI_MODEL`` -> default.

    The second step is load-bearing: ``relationship_runner`` used to read only
    ``GEMINI_BATCH_MODEL``, so setting just ``GEMINI_MODEL`` moved three pipelines and left
    relationship on the hardcoded default.
    """
    for name in ("GEMINI_BATCH_MODEL", "GEMINI_MODEL"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return DEFAULT_MODEL


def shard_size() -> int:
    """Rows per Batch job. Was also ``GSEARCH_GEMINI_CHUNK_SIZE`` — same number, two names."""
    return max(1, get_int_env("GEMINI_BATCH_SHARD_SIZE", DEFAULT_SHARD_SIZE))


def max_inflight() -> int:
    """Concurrent Batch jobs. Was also ``GSEARCH_GEMINI_MAX_INFLIGHT``."""
    return max(1, get_int_env("GEMINI_BATCH_MAX_INFLIGHT", DEFAULT_MAX_INFLIGHT))


def timeout_sec() -> int:
    """How long to wait for one Batch job. Was split across two keys with a 30min/48h split."""
    return max(60, get_int_env("GEMINI_BATCH_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC))


def poll_sec() -> int:
    """Seconds between polls of a running Batch job."""
    return max(5, get_int_env("GEMINI_BATCH_POLL_SEC", DEFAULT_POLL_SEC))
