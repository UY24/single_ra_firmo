"""Convenience launcher from the repo root — equivalent to:
  cd backend && ../.venv/bin/python -m app.main

Usage:
  python run.py                  # reads API_PORT / API_HOST / API_RELOAD from .env
  python run.py                  # with API_RELOAD=true → uvicorn hot-reload
"""
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.main import app  # noqa: E402  (chdir must happen first)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "11500"))
    reload = os.getenv("API_RELOAD", "").strip().lower() in {"1", "true", "yes", "on"}
    log_level = os.getenv("UVICORN_LOG_LEVEL", "info").strip().lower() or "info"
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
        reload_dirs=[str(BACKEND)] if reload else None,
        access_log=True,
        log_level=log_level,
    )
