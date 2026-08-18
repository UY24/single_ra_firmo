# backend/app/services/common/provider_limits.py
"""Shared provider concurrency gates.

scrape.do's concurrency cap is per **account**, and two independent pipelines hit it
from the same worker process — the gmaps pipeline (``serpwow/scrapedo_maps_client``)
and AI Mode (``ai_mode/worker``). Two separate limits could therefore sum past the
account cap, so they share ONE gate here.

This mirrors what already exists for the other provider: SerpWow calls are gated by
``serpwow_client.search_fetch_semaphore`` (sized by ``SEARCH_FETCH_CONCURRENCY``).
The knobs are per-provider, not per-pipeline, because that's what the vendors limit.

Rate limits: if scrape.do turns out to enforce requests-per-second on top of
concurrency, add a token bucket INSIDE ``scrapedo_slot`` — every caller already goes
through it, so no call site has to change.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from app.services.common.env import get_int_env

# Created lazily on first use so callers don't depend on startup ordering, and so a
# process that never touches scrape.do never builds one. Per-process by design: only
# the worker actually makes provider calls.
_scrapedo_semaphore: Optional[asyncio.Semaphore] = None
_scrapedo_limit: Optional[int] = None


def scrapedo_limit() -> int:
    """Concurrent scrape.do calls allowed across ALL pipelines in this process."""
    return max(1, get_int_env("SCRAPEDO_CONCURRENCY", 100))


@asynccontextmanager
async def scrapedo_slot():
    """Hold one scrape.do concurrency slot for the duration of the block.

    Wrap the actual HTTP call only — never a whole batch — or one slow row holds a
    slot it isn't using.
    """
    global _scrapedo_semaphore, _scrapedo_limit
    limit = scrapedo_limit()
    # Rebuild if the env changed between runs (tests, config reload).
    if _scrapedo_semaphore is None or _scrapedo_limit != limit:
        _scrapedo_semaphore = asyncio.Semaphore(limit)
        _scrapedo_limit = limit
    async with _scrapedo_semaphore:
        yield


def reset_scrapedo_limit() -> None:
    """Drop the cached semaphore (tests, or after changing SCRAPEDO_CONCURRENCY)."""
    global _scrapedo_semaphore, _scrapedo_limit
    _scrapedo_semaphore = None
    _scrapedo_limit = None
