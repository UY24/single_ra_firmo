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
            _client = create_client(url, key)
        else:
            logger.warning("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not set — "
                           "company tracking disabled")
    return _client
