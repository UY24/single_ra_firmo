"""FastAPI is the ONLY Supabase client (spec §4). Lazy singleton; None when unconfigured."""
from __future__ import annotations

import logging
import os
import threading
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
_client = None
_attempted = False
_config_error: str | None = None
_init_lock = threading.Lock()


def _redact_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        return "<invalid-url>"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _validate_config(url: str | None, key: str | None) -> tuple[str, str] | None:
    """Return normalized config or record a human-readable configuration error."""
    global _config_error
    _config_error = None
    url = (url or "").strip()
    key = (key or "").strip()
    missing = []
    if not url:
        missing.append("SUPABASE_URL")
    if not key:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    if missing:
        _config_error = f"missing {', '.join(missing)}"
        return None

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc.endswith(".supabase.co"):
        _config_error = (
            "invalid SUPABASE_URL: use the bare project REST URL "
            "(https://<project-ref>.supabase.co), not the postgres connection string"
        )
        logger.error("supabase: %s; got %s", _config_error, _redact_url(url))
        return None
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        _config_error = (
            "invalid SUPABASE_URL: remove paths/query strings; expected "
            "https://<project-ref>.supabase.co"
        )
        logger.error("supabase: %s; got %s", _config_error, _redact_url(url))
        return None
    if len(key) < 100 or key.count(".") != 2:
        _config_error = (
            "invalid SUPABASE_SERVICE_ROLE_KEY: paste the full service_role JWT "
            "from Supabase Project Settings > API"
        )
        logger.error("supabase: %s (length=%s, jwt_parts=%s)",
                     _config_error, len(key), key.count(".") + 1)
        return None
    return url.rstrip("/"), key


def get_supabase_config_error() -> str | None:
    return _config_error


def get_supabase():
    # FastAPI runs the sync company/ai-mode endpoints in a threadpool, and the
    # dashboard fires several in parallel on load. The init below is slow (first
    # `import supabase` + network handshake), so `_attempted` must flip to True
    # ONLY after `_client` is settled — otherwise a concurrent caller short-circuits
    # on the half-set flag, sees `_client is None`, and wrongly 503s "not configured"
    # (transient, clears on reload once init finishes). The lock + re-check serialize
    # the one-time init; the fast path stays lock-free once `_attempted` is True.
    global _client, _attempted
    if _attempted:
        return _client
    with _init_lock:
        if _attempted:
            return _client
        config = _validate_config(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
        if config:
            url, key = config
            from supabase import create_client
            from supabase.client import ClientOptions
            # Default postgrest timeout is 120s: an unreachable Supabase would
            # block endpoints ~2min per call (and per-upload locks for minutes
            # across update_run retries). Fail fast instead.
            logger.info("supabase: creating REST client for %s", _redact_url(url))
            _client = create_client(url, key, options=ClientOptions(
                postgrest_client_timeout=10,
                storage_client_timeout=10,
            ))
        else:
            logger.warning("supabase: company tracking disabled (%s)", _config_error)
        _attempted = True
    return _client
