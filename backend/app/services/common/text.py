# backend/app/services/common/text.py
"""Text helpers shared across pipelines (AI Mode + SerpWow)."""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional


def slugify_company(value: str) -> str:
    """Company folder slug shared by SerpWow and AI Mode.

    Lowercase, non-alphanumeric runs collapse to '-', trimmed. Guards ``None``.
    Used for the company SEGMENT of S3 keys + local run dirs so both pipelines
    share one company folder (e.g. "ISI Market Test" -> "isi-market-test").
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "unnamed"


def passthrough_fieldnames(
    header: Iterable[str],
    reserved: Iterable[str],
    source_overrides: Optional[Mapping[str, str]] = None,
) -> list[tuple[str, str]]:
    """``(output_name, source_name)`` for each ORIGINAL input-CSV column.

    Every output CSV is the user's own file plus the columns we computed, so the input
    header goes out first, verbatim and in order. This resolves the two ways that can
    collide:

    - against a name we write ourselves (``reserved``) or an earlier duplicate in the
      header — the passthrough copy gets an ``__orig`` suffix (and another for a further
      collision) rather than being dropped. Without this, ``row.update()``'s computed
      value silently overwrites the user's cell.
    - against a key the reader injected: ``s3_run_store.iter_input_rows`` overwrites a
      real ``row_index`` column with its own int index and parks the original under
      ``row_index__input``, so the S3 pipelines pass that mapping as
      ``source_overrides``. Readers that inject nothing (AI Mode's plain DictReader)
      pass none.

    Lives here, not in reporting, because ai_mode/run_reporting.py is
    deliberately standalone (stdlib + models only) and that module pulls in httpx.
    """
    overrides = source_overrides or {}
    seen = set(reserved)
    out: list[tuple[str, str]] = []
    for name in header:
        out_name = name
        while out_name in seen:
            out_name = f"{out_name}__orig"
        seen.add(out_name)
        out.append((out_name, overrides.get(name, name)))
    return out


def passthrough_row(original: Mapping[str, Any],
                    passthrough: Iterable[tuple[str, str]]) -> dict[str, str]:
    """The original row's cells, keyed for output. Missing cells become "" — a short
    row must not shift the columns of the row after it."""
    return {out: str(original.get(src, "") or "") for out, src in passthrough}
