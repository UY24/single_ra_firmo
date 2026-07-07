# backend/app/services/common/text.py
"""Text helpers shared across pipelines (AI Mode + SerpWow)."""
from __future__ import annotations

import re


def slugify_company(value: str) -> str:
    """Company folder slug shared by SerpWow and AI Mode.

    Lowercase, non-alphanumeric runs collapse to '-', trimmed. Guards ``None``.
    Used for the company SEGMENT of S3 keys + local run dirs so both pipelines
    share one company folder (e.g. "ISI Market Test" -> "isi-market-test").
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "unnamed"
