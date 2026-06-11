# backend/app/main.py
"""App entrypoint. The SerpWow monolith still owns the FastAPI instance; new
routers are attached to it here. Run: cd backend && ../.venv/bin/python -m app.main"""
import os

from app.core import config  # noqa: F401  (loads .env first)
from app.services.serpwow.legacy_app import _get_int_env, app  # the existing FastAPI instance
from app.routers.ai_mode import router as ai_mode_router

app.include_router(ai_mode_router)

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("API_HOST", "0.0.0.0")
    port = _get_int_env("API_PORT", 11500)
    reload = os.getenv("API_RELOAD", "").strip().lower() in {"1", "true", "yes", "on"}
    log_level = os.getenv("UVICORN_LOG_LEVEL", "info").strip().lower() or "info"
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
        access_log=True,
        log_level=log_level,
    )
