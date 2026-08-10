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
