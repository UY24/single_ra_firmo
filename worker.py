"""Run the SerpWow RabbitMQ worker from the repo root.

Command:
  python worker.py
"""
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.services.serpwow.worker import main  # noqa: E402


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
