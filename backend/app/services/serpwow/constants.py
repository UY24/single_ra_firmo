# backend/app/services/serpwow/constants.py
"""Pipeline identifiers shared across SerpWow modes."""
from __future__ import annotations

PIPELINE_FIRMOGRAPHICS = "firmographics"
PIPELINE_GMAPS = "gmaps"
PIPELINE_GSEARCH = "gsearch"
PIPELINE_RELATIONSHIP = "relationship"

# Pipelines whose STATE-DRIVEN runs produce found/notFound/report/run.log via
# serpwow_reporting. gmaps is deliberately absent since 2026-08: it has no state.json at
# all any more, so it writes those same four files from gmaps_outputs instead, streaming
# rather than materialising every row.
REPORTING_PIPELINES = {PIPELINE_GSEARCH, PIPELINE_RELATIONSHIP}

# Relationship-mode row-error strings (user-visible in notFound.csv / UI).
REL_ERROR_NO_EVIDENCE = "No search evidence found for this company pair."
REL_ERROR_NO_X = "Company X missing — financial relationship cannot be verified."
REL_ERROR_NOT_CONFIRMED = "No financial relationship confirmed between Company X and Company Y."
REL_ERROR_CONFIRMED_URL_INVALID = "Relationship confirmed but no valid candidate URL survived validation."
