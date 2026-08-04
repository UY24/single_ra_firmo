# backend/app/core/s3.py
"""Shared S3 helpers — bucket from S3_BUCKET, region from S3_REGION.

Used by AI Mode to mirror a completed run directory to S3. SerpWow keeps its own
client (see services/serpwow/engine.py); this module is intentionally small.
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Iterator

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
                # "adaptive" adds AWS's client-side rate limiter, which is their documented answer to
                # S3 SlowDown (HTTP 503): it backs off proactively instead of just retrying harder.
                retries={"max_attempts": max(1, _get_int_env("S3_MAX_RETRIES", 3)),
                         "mode": "adaptive"},
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


def iter_keys(prefix: str = "") -> Iterator[str]:
    """Yield every object key under ``prefix`` (whole bucket when prefix is empty)."""
    paginator = get_s3_client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name(), Prefix=prefix):
        for obj in page.get("Contents", []):
            yield obj["Key"]


def download_file(key: str, local_path: Path) -> None:
    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    get_s3_client().download_file(bucket_name(), key, str(local_path))
