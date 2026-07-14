"""Mirror a completed AI Mode run dir to S3 under <company>/<mode>/<run_id>/.

Never raises: S3 failures are logged and swallowed so a run always completes on
disk (matches SerpWow's never-fail-the-run design).
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.core import s3
from app.core.config import AI_MODE_RESULTS_DIR

_LOGGER = logging.getLogger("ai_mode")


def s3_key_prefix(run_dir: Path, mode_key: str) -> str:
    """run_dir is ai_mode_results/<company_slug>/<run_id>; build that prefix + mode."""
    return f"{run_dir.parent.name}/{mode_key}/{run_dir.name}"


def run_s3_uri(run_dir: Path, mode_key: str, filename: str) -> str | None:
    """``s3://<bucket>/<company>/<mode>/<run_id>/<filename>`` or None if S3 unset."""
    if not s3.is_configured():
        return None
    return f"s3://{s3.bucket_name()}/{s3_key_prefix(run_dir, mode_key)}/{filename}"


def mirror_file_to_s3(run_dir: Path, mode_key: str, local_path: Path) -> bool:
    """Write-through one file to S3 under the run's prefix, as it's produced.

    Best-effort: no-ops when S3 is unconfigured, swallows+logs all errors, never
    raises (must not slow or fail the scrape/clean hot path). Mirrors SerpWow's
    per-row upload pattern so a hard-killed run (OOM / SIGKILL / spot reclaim that
    bypasses the end-of-run mirror) is still resumable from S3.
    """
    if not s3.is_configured():
        return False
    local_path = Path(local_path)
    try:
        rel = local_path.relative_to(run_dir).as_posix()
    except ValueError:
        return False
    key = f"{s3_key_prefix(run_dir, mode_key)}/{rel}"
    try:
        s3.upload_file(local_path, key)
        return True
    except Exception as exc:  # never fail the run on a write-through error
        _LOGGER.warning("S3 write-through failed for %s: %s: %s",
                        key, type(exc).__name__, exc)
        return False


def delete_mirrored_file(run_dir: Path, mode_key: str, local_path: Path) -> bool:
    """Best-effort delete of one file's S3 mirror (e.g. error markers cleared on
    resume, so a later rehydrate can't resurrect them). Never raises."""
    if not s3.is_configured():
        return False
    try:
        rel = Path(local_path).relative_to(run_dir).as_posix()
    except ValueError:
        return False
    key = f"{s3_key_prefix(run_dir, mode_key)}/{rel}"
    try:
        s3.get_s3_client().delete_object(Bucket=s3.bucket_name(), Key=key)
        return True
    except Exception as exc:  # never fail the caller on a mirror delete
        _LOGGER.warning("S3 delete failed for %s: %s: %s",
                        key, type(exc).__name__, exc)
        return False


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


def rehydrate_run_from_s3(run_id: str) -> Path | None:
    """Restore a run's mirrored files from S3 back to local disk.

    For hosted/ephemeral storage: if the local run dir was wiped (instance
    replaced), resume can't reuse ``raw_responses/``/``cleaned/`` — so pull them
    back first. S3 keys are ``<company>/<mode>/<run_id>/<relpath>``; the local
    layout drops the ``<mode>`` segment → ``AI_MODE_RESULTS_DIR/<company>/<run_id>/``.

    Returns the restored local run dir, or None if S3 is unconfigured, nothing
    matched, or the download failed (best-effort — never raises).
    """
    if not s3.is_configured():
        return None
    needle = f"/{run_id}/"
    target_dir: Path | None = None
    restored = 0
    failed = 0
    try:
        for key in s3.iter_keys():
            if needle not in f"/{key}":
                continue
            parts = key.split("/")
            if run_id not in parts:
                continue
            idx = parts.index(run_id)
            if idx < 1:  # need a leading <company> segment
                continue
            rel_parts = parts[idx + 1:]
            if not rel_parts:  # a folder marker, not a file
                continue
            local_dir = AI_MODE_RESULTS_DIR / parts[0] / run_id
            try:
                s3.download_file(key, local_dir.joinpath(*rel_parts))
                restored += 1
                target_dir = local_dir
            except Exception as exc:  # skip one bad object, keep restoring the rest
                failed += 1
                _LOGGER.warning("S3 rehydrate: skipped %s: %s: %s",
                                key, type(exc).__name__, exc)
    except Exception as exc:  # listing/iteration error — keep whatever we restored
        _LOGGER.error("S3 rehydrate listing failed for run %s: %s: %s",
                      run_id, type(exc).__name__, exc)
    if restored:
        # A partial restore is fine: resume re-scrapes/re-cleans only the batches
        # whose files didn't come back, so output stays correct.
        _LOGGER.info("Rehydrated %d file(s) for run %s from S3 to %s%s",
                     restored, run_id, target_dir,
                     f" ({failed} failed)" if failed else "")
        return target_dir
    _LOGGER.info("No S3 objects restored for run %s", run_id)
    return None
