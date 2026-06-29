"""Test package init.

Hard safety net: blank SLACK_WEBHOOK_URL for the entire test process so no test
that exercises a real run path (e.g. run_ai_mode_sync / persist_upload_state) can
POST to a real Slack webhook. This runs before the app's app.core.config calls
load_dotenv() — which uses override=False, so it will NOT re-populate this value
from .env. Tests that specifically need a webhook set it explicitly via
mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": ...}, clear=True).
"""
import os

os.environ["SLACK_WEBHOOK_URL"] = ""
