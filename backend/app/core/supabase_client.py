"""FastAPI is the ONLY Supabase client (spec §4). Lazy singleton; None when unconfigured."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_client = None
_attempted = False


def get_supabase():
    global _client, _attempted
    if not _attempted:
        _attempted = True
        url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if url and key:
            from supabase import create_client
            from supabase.client import ClientOptions
            # Default postgrest timeout is 120s: an unreachable Supabase would
            # block endpoints ~2min per call (and per-upload locks for minutes
            # across update_run retries). Fail fast instead.
            _client = create_client(url, key, options=ClientOptions(
                postgrest_client_timeout=10,
                storage_client_timeout=10,
            ))
        else:
            logger.warning("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not set — "
                           "company tracking disabled")
    return _client
