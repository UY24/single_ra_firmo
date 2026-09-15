# backend/app/services/serpwow/constants.py
"""Pipeline identifiers and per-pipeline policy shared across the upload modes.

Batch-vs-inline policy is NOT here: it lives in ``common/llm_batch.py``, which both this
package and ``ai_mode`` import.
"""
from __future__ import annotations

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
