# backend/app/core/s3.py
"""Shared S3 helpers — bucket from S3_BUCKET, region from S3_REGION.

Used by AI Mode to mirror a completed run directory to S3. SerpWow keeps its own
client (see services/serpwow/legacy_app.py); this module is intentionally small.
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig

_client = None


def _get_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def get_s3_client():
    """Lazy boto3 S3 client; region + timeout knobs mirror the SerpWow client."""
    global _client
    if _client is None:
        region = os.getenv("S3_REGION") or "ap-south-1"
        _client = boto3.client(
            "s3",
            region_name=region,
            config=BotoConfig(
                connect_timeout=_get_int_env("S3_CONNECT_TIMEOUT_SEC", 15),
                read_timeout=_get_int_env("S3_READ_TIMEOUT_SEC", 90),
                retries={"max_attempts": max(1, _get_int_env("S3_MAX_RETRIES", 3)),
                         "mode": "standard"},
                max_pool_connections=100,
            ),
        )
    return _client


def is_configured() -> bool:
    return bool(os.getenv("S3_BUCKET"))


def bucket_name() -> str:
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET not configured")
    return bucket


def upload_file(local_path: Path, key: str) -> None:
    content_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
    get_s3_client().upload_file(
        str(local_path), bucket_name(), key,
        ExtraArgs={"ContentType": content_type},
    )


def upload_directory(local_dir: Path, key_prefix: str) -> list[str]:
    """Upload every file under ``local_dir`` to ``key_prefix/<relative posix path>``.

    Returns the list of keys written, in sorted order. Preserves subfolders
    (e.g. ``raw_responses/``).
    """
    local_dir = Path(local_dir)
    prefix = key_prefix.rstrip("/")
    keys: list[str] = []
    for path in sorted(local_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_dir).as_posix()
        key = f"{prefix}/{rel}"
        upload_file(path, key)
        keys.append(key)
    return keys
