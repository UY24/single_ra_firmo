"""Mirror a completed AI Mode run dir to S3 under <company>/<mode>/<run_id>/.

Never raises: S3 failures are logged and swallowed so a run always completes on
disk (matches SerpWow's never-fail-the-run design).
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.core import s3

_LOGGER = logging.getLogger("ai_mode")


def s3_key_prefix(run_dir: Path, mode_key: str) -> str:
    """run_dir is ai_mode_results/<company_slug>/<run_id>; build that prefix + mode."""
    return f"{run_dir.parent.name}/{mode_key}/{run_dir.name}"


def mirror_run_to_s3(run_dir: Path, mode_key: str) -> list[str]:
    if not s3.is_configured():
        _LOGGER.info("S3 not configured; skipping mirror for run %s", run_dir.name)
        return []
    prefix = s3_key_prefix(run_dir, mode_key)
    try:
        keys = s3.upload_directory(run_dir, prefix)
        _LOGGER.info("Mirrored %d file(s) to s3://%s/%s",
                     len(keys), s3.bucket_name(), prefix)
        return keys
    except Exception as exc:  # never fail the run on S3 errors
        _LOGGER.error("S3 mirror failed for %s: %s: %s",
                      prefix, type(exc).__name__, exc)
        return []
