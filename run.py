"""Convenience launcher: `python run.py` (or .venv/bin/python run.py) from the repo root.
Equivalent to: cd backend && ../.venv/bin/python -m app.main
Port/host come from .env (API_PORT, API_HOST).

Note: the launch is guarded by `if __name__ == "__main__"` so that uvicorn's
API_RELOAD=true reloader (which re-imports this module in its spawned worker as
__mp_main__) does not start a second server — without the guard the reload
worker dies with "Address already in use".
"""
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    BACKEND = Path(__file__).resolve().parent / "backend"
    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)

    import runpy
    runpy.run_module("app.main", run_name="__main__")
