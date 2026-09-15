"""Test package init.

Hard safety net: blank every cloud/credential env var for the ENTIRE test
process so no test that exercises a real run path (run_ai_mode_finish /
persist_upload_state / run_gemini_batch_for_upload) can hit a live endpoint.
This runs before app.core.config's load_dotenv(override=False), which will NOT
re-populate these. Tests needing a value set it explicitly via
mock.patch.dict(os.environ, {...}, clear=True).
"""
import os

# Hard safety net: blank every cloud/credential env var for the ENTIRE test
# process so no test that exercises a real run path (run_ai_mode_finish /
# persist_upload_state / run_gemini_batch_for_upload) can hit a live endpoint.
# This runs before app.core.config's load_dotenv(override=False), which will NOT
# re-populate these. Tests needing a value set it explicitly via
# mock.patch.dict(os.environ, {...}, clear=True).
for _key in (
    "SLACK_WEBHOOK_URL", "S3_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "GEMINI_API_KEY", "SERPWOW_API_KEY",
    # AI Mode's scraper, the gmaps pipeline's Google Maps client, and (since the 2026-08
    # migration) the relationship pipeline's AI Mode client all read this — without it a
    # row-path test could hit live scrape.do.
    "SCRAPEDO_TOKEN",
):
    os.environ[_key] = ""

# BEHAVIOUR toggles, not credentials — but they belong here for the same reason, and one
# of them bit: a developer .env with LLM_BATCH=true flipped AI Mode's cleanup into the
# Gemini BATCH path during an "offline" test, which then made a real HTTPS call to
# generativelanguage.googleapis.com and failed with HTTP 400. Blanking a credential stops
# a leak; blanking these stops the suite's RESULT depending on whose machine it runs on.
#
# Blanked, not deleted, so llm_batch's blank-counts-as-unset rule reads it as unset: a test
# that wants batching on sets LLM_BATCH explicitly, which is also what documents its intent.
# Since 2026-08-20 that is the ONLY batch toggle, so the whole hazard rests on this one key
# (the per-pipeline overrides that used to be blanked alongside it are deleted).
# Batch SIZES are here too — AI_DEEP_BATCH_SIZE=2 in a .env silently changed how many
# batches a fixture produced and broke four assertions that had nothing to do with it.
for _key in (
    "LLM_BATCH",
    "AI_BULK_BATCH_SIZE", "AI_DEEP_BATCH_SIZE",
    "GEMINI_BATCH_SHARD_SIZE", "GEMINI_BATCH_MAX_INFLIGHT", "GEMINI_BATCH_MODEL",
):
    os.environ[_key] = ""
