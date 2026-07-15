# backend/app/services/serpwow/constants.py
"""Pipeline identifiers shared across SerpWow modes."""
from __future__ import annotations

PIPELINE_FULL = "full"
PIPELINE_URL_DISCOVERY = "url_discovery"
PIPELINE_FIRMOGRAPHICS = "firmographics"
PIPELINE_GMAPS = "gmaps"
PIPELINE_GSEARCH = "gsearch"
PIPELINE_RELATIONSHIP = "relationship"

# Pipelines that produce found/notFound/report/run.log via serpwow_reporting
# and surface a serpwow_summary block (gsearch parity).
REPORTING_PIPELINES = {PIPELINE_GSEARCH, PIPELINE_GMAPS, PIPELINE_RELATIONSHIP}

# Relationship-mode row-error strings (user-visible in notFound.csv / UI).
REL_ERROR_NO_EVIDENCE = "No search evidence found for this company pair."
REL_ERROR_NO_X = "Company X missing — financial relationship cannot be verified."
REL_ERROR_NOT_CONFIRMED = "No financial relationship confirmed between Company X and Company Y."
REL_ERROR_CONFIRMED_URL_INVALID = "Relationship confirmed but no valid candidate URL survived validation."

# Stable machine-readable relationship outcome reasons. Keep these separate from
# the user-facing REL_ERROR_* strings, which are persisted in existing artifacts.
REL_REASON_MISSING_X = "missing_company_x"
REL_REASON_NO_EVIDENCE = "no_search_evidence"
REL_REASON_NOT_CONFIRMED = "relationship_not_confirmed"
REL_REASON_UNCLEAR = "relationship_unclear"
REL_REASON_CONFIRMED_URL_NOT_VALIDATED = "confirmed_relationship_url_not_validated"


def relationship_reason_code(
    website_url: object,
    status: object,
    row_error: object = None,
    *,
    technical_failure: bool = False,
) -> str:
    """Return the stable reason for a finalized relationship result."""
    if str(website_url or "").strip() or technical_failure:
        return ""
    by_error = {
        REL_ERROR_NO_X: REL_REASON_MISSING_X,
        REL_ERROR_NO_EVIDENCE: REL_REASON_NO_EVIDENCE,
    }
    error_reason = by_error.get(str(row_error or "").strip())
    if error_reason:
        return error_reason
    return {
        "confirmed": REL_REASON_CONFIRMED_URL_NOT_VALIDATED,
        "not_confirmed": REL_REASON_NOT_CONFIRMED,
        "unclear": REL_REASON_UNCLEAR,
    }.get(str(status or "").strip(), "")
