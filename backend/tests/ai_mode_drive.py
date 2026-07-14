# backend/tests/ai_mode_drive.py
"""Offline harness: drive an AI Mode run through the broker engine, no RabbitMQ.

Emulates the production flow — publish_run_batches captures every message
in-process, then each payload is fed through the worker's process_scrape_job
exactly as the consumer loop would, and any finish task dispatched by the
barrier is awaited. Callers must patch the external seams (ScrapeDoClient,
make_llm_client / gemini_batch) and call ai_worker._reset_for_tests() around
each test.
"""
import asyncio
from unittest import mock

from app.services.ai_mode import worker as ai_worker


def drive_run(run_id: str, *, only_missing: bool = False) -> list[dict]:
    """Publish + consume + finish a prepared run entirely in-process.

    Returns the captured message payloads (scrape messages + trailing check).
    """
    return asyncio.run(_drive(run_id, only_missing=only_missing))


async def _drive(run_id: str, *, only_missing: bool = False) -> list[dict]:
    published: list[dict] = []

    async def capture(payload):
        published.append(payload)

    with mock.patch.object(ai_worker.broker, "publish_scrape_job", side_effect=capture):
        await ai_worker.publish_run_batches(run_id, only_missing=only_missing)
        for payload in list(published):
            await ai_worker.process_scrape_job(payload)
        tasks = list(ai_worker._finish_tasks.values())
        if tasks:
            await asyncio.gather(*tasks)
    return published
