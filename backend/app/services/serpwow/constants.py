# backend/app/services/serpwow/constants.py
"""Pipeline identifiers and per-pipeline policy shared across the upload modes."""
from __future__ import annotations

import os

from app.services.common.env import get_bool_env

PIPELINE_FIRMOGRAPHICS = "firmographics"
PIPELINE_GMAPS = "gmaps"
PIPELINE_GSEARCH = "gsearch"
PIPELINE_RELATIONSHIP = "relationship"

# Pipelines whose STATE-DRIVEN runs produce found/notFound/report/run.log via
# reporting. gmaps is deliberately absent since 2026-08: it has no state.json at
# all any more, so it writes those same four files from gmaps_outputs instead, streaming
# rather than materialising every row.
REPORTING_PIPELINES = {PIPELINE_GSEARCH, PIPELINE_RELATIONSHIP}

# Pipelines that get a cost/call summary on /status, in Supabase and in the Slack ping.
# A SUPERSET of REPORTING_PIPELINES: firmographics is here but NOT above, because it
# needs the billing card (it spends scrape.do credits per row) while its found/notFound
# split would be meaningless -- it is handed the website, so every answered row is
# trivially "found". Keeping the two sets apart is what lets it report money without
# claiming a discovery result it never computed.
COST_SUMMARY_PIPELINES = REPORTING_PIPELINES | {PIPELINE_FIRMOGRAPHICS}

# Relationship-mode row-error strings (user-visible in notFound.csv / UI).
REL_ERROR_NO_EVIDENCE = "No search evidence found for this company pair."
REL_ERROR_NO_X = "Company X missing — financial relationship cannot be verified."
REL_ERROR_NOT_CONFIRMED = "No financial relationship confirmed between Company X and Company Y."
REL_ERROR_CONFIRMED_URL_INVALID = "Relationship confirmed but no valid candidate URL survived validation."


def batch_postprocess_enabled_for(pipeline: str) -> bool:
    """True when this pipeline's Gemini work should run as a BATCH job instead of inline.

    ONE definition, imported by ``engine`` (which seeds and drives the batch) and by the
    mode executors (which must then skip their inline call, or the run pays twice). It used
    to live in ``engine`` with a second copy of the env read inside ``modes/gsearch``, which
    is exactly how the two drift.

    ``FIRMOGRAPHICS_LLM_BATCH`` defaults to whatever ``GSEARCH_LLM_BATCH`` is set to, so ONE
    toggle switches batching for both; set the firmographics key only to make them differ.
    That fallback mirrors ``AI_MODE_WORKER_CONCURRENCY`` -> ``WORKER_CONCURRENCY``.

    relationship is absent on purpose: it owns its own Gemini Batch driver in
    ``relationship_runner``. gmaps has no LLM at all.
    """
    pipe = str(pipeline or "")
    gsearch_batch = get_bool_env("GSEARCH_LLM_BATCH", False)
    if pipe == PIPELINE_GSEARCH:
        return gsearch_batch
    if pipe == PIPELINE_FIRMOGRAPHICS:
        # BLANK counts as unset, so the GSEARCH_LLM_BATCH fallback still applies.
        # get_bool_env only falls back when the variable is ABSENT, and .env.example ships
        # every key as `NAME=` -- so a blank line would have silently overridden the
        # fallback to False and made "one toggle for both" quietly untrue.
        override = os.getenv("FIRMOGRAPHICS_LLM_BATCH")
        if override is None or not override.strip():
            return gsearch_batch
        return get_bool_env("FIRMOGRAPHICS_LLM_BATCH", gsearch_batch)
    return False
