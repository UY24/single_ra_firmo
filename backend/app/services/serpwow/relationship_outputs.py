# backend/app/services/serpwow/relationship_outputs.py
"""Phase 3: stream cleaned/ + input.csv into the run's output files.

Memory is O(one row): each row is read, gated, written, and dropped. report.json carries
the SUMMARY ONLY — at 500k rows a per-row array would be gigabytes, and the two CSVs
already hold the per-row detail.

Outputs are split by RELATIONSHIP STATUS, not URL presence: everything that is not
"confirmed" (not_confirmed, unclear, and any error row) goes to notconfirmed_relation.csv.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from app.services.serpwow import relationship_store as store
from app.services.serpwow.modes.relationship import build_row_result, row_fields

EXTRA_COLUMNS = [
    "website_url", "resolved_company_y_name", "relationship_status",
    "relationship_summary", "relationship_evidence", "relationship_confidence",
    "website_confidence", "confidence", "flags", "error_source", "error_reason",
]


def _flags_csv(relationship: dict[str, Any]) -> str:
    flags = relationship.get("flags") or []
    return "\n".join(
        f"{f.get('flag')}: {f.get('why')}" for f in flags if isinstance(f, dict))


def _out_row(original: dict[str, str], header: list[str],
             result: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    rel = result["relationship"]
    evidence = rel.get("evidence") or parsed.get("relationship_evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    row = {h: str(original.get(h, "") or "") for h in header}
    row.update({
        "website_url": result["official_website"],
        "resolved_company_y_name": str(rel.get("resolved_company_y_name")
                                       or parsed.get("resolved_company_y_name") or ""),
        "relationship_status": result["relationship_status"],
        "relationship_summary": str(rel.get("summary") or ""),
        "relationship_evidence": "\n".join(
            str(i) for i in evidence if str(i).strip()),
        "relationship_confidence": int(rel.get("relationship_confidence_score") or 0),
        "website_confidence": int(rel.get("website_confidence_score") or 0),
        "confidence": int(parsed.get("confidence_score") or 0),
        "flags": _flags_csv(rel),
        "error_source": result["error_source"],
        "error_reason": result["row_error"],
    })
    return row


def write_outputs(prefix: str, counters: store.Counters) -> dict[str, Any]:
    """Write the two CSVs, report.json and run.log to S3. Returns the summary."""
    counters.set_phase("reporting")
    counters.flush(force=True)

    header = store.read_input_header(prefix)
    fieldnames = header + EXTRA_COLUMNS

    # StringIO rather than a list of rows: the CSV text is built incrementally and the
    # per-row objects are dropped as we go, so nothing scales with the run except the
    # finished CSV text itself (streamed out in one PUT at the end).
    buffers = {name: io.StringIO() for name in
               ("confirmed_relation.csv", "notconfirmed_relation.csv")}
    writers = {}
    for name, buf in buffers.items():
        buf.write("﻿")  # BOM: Excel reads a BOM-less UTF-8 CSV as Mac Roman
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writers[name] = writer

    log_lines: list[str] = []
    counts = {"confirmed": 0, "not_confirmed": 0, "unclear": 0}
    outcomes = {"found": 0, "not_found": 0, "errored": 0}
    by_source: dict[str, int] = {}
    requests = successes = credits = billed_empty = error_requests = 0
    total_rows = found = 0

    for original in store.iter_input_rows(prefix):
        total_rows += 1
        idx = int(original["row_index"])
        fields = row_fields(original)

        envelope = store.get_object(store.raw_key(prefix, idx))
        if envelope is None:
            envelope = store.get_object(store.error_key(prefix, idx)) or {
                "error": "Row was never processed.", "error_category": "internal"}

        requests += int(envelope.get("request_count") or 0)
        successes += int(envelope.get("successful_requests") or 0)
        credits += int(envelope.get("credits") or 0)
        if envelope.get("billed_empty"):
            billed_empty += 1
        if envelope.get("error"):
            error_requests += int(envelope.get("request_count") or 0)

        cleaned = store.get_object(store.cleaned_key(prefix, idx)) or {}
        parsed = cleaned.get("parsed")
        result = build_row_result(
            fields, envelope, parsed if isinstance(parsed, dict) else None,
            cleaned.get("candidates") or [], str(cleaned.get("x_domain") or ""))

        status = result["relationship_status"]
        counts[status] = counts.get(status, 0) + 1
        if envelope.get("error"):
            outcomes["errored"] += 1
            by_source["scrapedo"] = by_source.get("scrapedo", 0) + 1
        elif result["official_website"]:
            outcomes["found"] += 1
            found += 1
        else:
            outcomes["not_found"] += 1

        name = ("confirmed_relation.csv" if status == "confirmed"
                else "notconfirmed_relation.csv")
        writers[name].writerow(
            _out_row(original, header, result,
                     parsed if isinstance(parsed, dict) else {}))

        log_lines.append(
            f"[{idx}] {fields['y_name']} -> "
            f"{result['official_website'] or 'not found'} ({status})"
            + (f" — {result['row_error']}" if result["row_error"] else ""))

    summary = {
        "pipeline": "relationship",
        "total_rows": total_rows,
        "websites_found": found,
        "websites_not_found": total_rows - found,
        "relationship_breakdown": counts,
        "outcome_breakdown": outcomes,
        "error_breakdown": {"by_source": by_source, "by_category": {}},
        "empty_response_breakdown": {"empty": billed_empty},
        "confidence_mode": "llm",
        "cost": {
            # scrape.do bills CREDITS, never dollars. No serpwow_* key appears here —
            # its absence is what routes this run down the scrape.do branch in the UI.
            "scrapedo_requests": requests,
            "scrapedo_successful_requests": successes,
            "scrapedo_failed_requests": max(0, requests - successes),
            "scrapedo_error_requests": error_requests,
            "scrapedo_billed_empty": billed_empty,
            "scrapedo_credits": credits,
            "llm_usd": 0.0,
            "total_usd": 0.0,
        },
    }

    for name, buf in buffers.items():
        store.put_bytes(f"{prefix}/{name}", buf.getvalue().encode("utf-8"),
                        content_type="text/csv")
    store.put_bytes(
        f"{prefix}/report.json",
        json.dumps({"summary": summary}, ensure_ascii=False, indent=2).encode("utf-8"),
        content_type="application/json")

    hdr = [
        f"# relationship run — status=completed",
        f"# rows={total_rows} found={found} not_found={total_rows - found}",
        (f"# cost: scrapedo_requests={requests} (ok={successes} "
         f"errors={error_requests}) scrapedo_credits={credits} "
         f"scrapedo_billed_empty={billed_empty}"),
        "",
    ]
    store.put_bytes(f"{prefix}/run.log",
                    ("\n".join(hdr + log_lines) + "\n").encode("utf-8"),
                    content_type="text/plain")

    counters.set_phase("completed")
    counters.flush(force=True)
    return summary
