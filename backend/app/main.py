# backend/app/main.py
"""App entrypoint. The SerpWow monolith still owns the FastAPI instance; new
routers are attached to it here. Run: cd backend && ../.venv/bin/python -m app.main"""
import os

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core import config  # noqa: F401  (loads .env first)
from app.core.config import STATIC_DIR, TEMPLATES_DIR
from app.services.serpwow.engine import _get_int_env, app  # the existing FastAPI instance
from app.routers.ai_mode import router as ai_mode_router
from app.routers.companies import router as companies_router

app.include_router(ai_mode_router)
app.include_router(companies_router)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/app")
def serve_app():
    return FileResponse(str(TEMPLATES_DIR / "index.html"))

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
