# backend/app/core/config.py
"""Single source of truth for paths and env loading."""
from pathlib import Path
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent          # backend/app
BACKEND_DIR = APP_DIR.parent                              # backend
PROJECT_ROOT = BACKEND_DIR.parent                         # website_url_finder
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
PROMPTS_DIR = APP_DIR / "prompts"
AI_MODE_RESULTS_DIR = PROJECT_ROOT / "ai_mode_results"    # new layout (used in a later task)
LEGACY_AI_MODE_RESULT_DIR = PROJECT_ROOT / "ai_mode_result"

load_dotenv(PROJECT_ROOT / ".env")
