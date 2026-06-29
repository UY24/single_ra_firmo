# backend/app/services/serpwow/gsearch_reporting.py
"""found.csv / notFound.csv / report.json / run.log for the gsearch pipeline.

Converts the terminal SerpWow upload ``state["rows"]`` (each row carries a
CrawlResponse dict under ``result``) into the shared EntityResult schema, then
writes the same file set AI Mode produces so the Runs UI can view them uniformly.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from app.models.results import AttemptLogEntry, EntityResult, Flag

CSV_COLUMNS = ["company_name", "company_local_name", "country", "website_url",
               "confidence", "flags", "attempt_log"]


def _confidence_raw(result: dict[str, Any]) -> dict[str, Any]:
    ctx = result.get("context") or {}
    for key in ("final_url_selection_ai", "gemini_batch_ai"):
        obj = ctx.get(key)
        if isinstance(obj, dict) and isinstance(obj.get("raw"), dict):
            return obj["raw"]
    return {}


def row_to_entity_result(row: dict[str, Any], sno: int) -> EntityResult:
    result = row.get("result") or {}
    ctx = result.get("context") or {}
    raw = _confidence_raw(result)

    # Authoritative website = the row's validated official_website (matches
    # row["status"] / the success/failed counts). The raw LLM pick can hold a URL
    # that was rejected (e.g. invented / out-of-candidate) where result.official_website
    # is None — so reading raw here would wrongly count it as found. Use result.
    website = result.get("official_website") or None
    try:
        confidence = max(0, min(100, int(raw.get("confidence_score") or 0)))
    except (TypeError, ValueError):
        confidence = 0

    flags: list[Flag] = []
    band = str(raw.get("confidence") or "").strip()
    if band:
        flags.append(Flag("confidence_band", band))
    reason = str(raw.get("reason") or "").strip()
    if reason:
        flags.append(Flag("reason", reason))
    if raw.get("domain_name_mismatch"):
        flags.append(Flag("domain_name_mismatch",
                          "chosen domain doesn't obviously match the company name — verify"))
    for alt in (raw.get("alternatives") or [])[:5]:
        if alt:
            flags.append(Flag("alternative", str(alt)))

    attempts: list[AttemptLogEntry] = []
    for fr in ctx.get("formatted_results") or []:
        if not isinstance(fr, dict):
            continue
        outcome = "ok" if fr.get("success") else (fr.get("error") or "no result")
        attempts.append(AttemptLogEntry(
            query=f"[{fr.get('phase')}] {fr.get('query')}",
            result=str(outcome), url=fr.get("search_url")))

    return EntityResult(
        company_name=str(row.get("company_name") or ""),
        country=str(row.get("country") or ""),
        sno=sno,
        company_local_name=row.get("company_local_name") or row.get("local_name"),
        website_url=website,
        confidence=confidence,
        flags=flags,
        attempt_log=attempts,
        error=row.get("error"),
    )


def state_to_entity_results(state: dict[str, Any]) -> list[EntityResult]:
    return [row_to_entity_result(r, i + 1) for i, r in enumerate(state.get("rows", []))]


def build_summary(state: dict[str, Any], results: list[EntityResult]) -> dict[str, Any]:
    found = sum(1 for r in results if r.website_url)
    serpwow_searches = 0
    llm_usd = 0.0
    prompt_tokens = 0
    completion_tokens = 0
    model: str | None = None
    for row in state.get("rows", []):
        result = row.get("result") or {}
        ctx = result.get("context") or {}
        cb = ctx.get("cost_breakdown") or {}
        serpwow_searches += int(cb.get("serpwow_request_count") or 0)
        llm_usd += float(result.get("gemini_cost_usd") or 0.0)
        for key in ("final_url_selection_ai", "gemini_batch_ai"):
            obj = ctx.get(key)
            if isinstance(obj, dict):
                if model is None and obj.get("model"):
                    model = str(obj.get("model"))
                usage = obj.get("usage")
                if isinstance(usage, dict):
                    prompt_tokens += int(usage.get("promptTokenCount", 0) or 0)
                    completion_tokens += int(usage.get("candidatesTokenCount", 0) or 0)
    # is_batch: the gemini_batch block is only seeded when batch post-processing is
    # enabled for this upload (GSEARCH_LLM_BATCH), so its presence is the reliable signal.
    is_batch = bool(state.get("gemini_batch"))
    return {
        "upload_id": state.get("upload_id"),
        "company_name": state.get("company_name"),
        "pipeline": state.get("pipeline"),
        "status": state.get("status"),
        "total_rows": len(results),
        "websites_found": found,
        "websites_not_found": len(results) - found,
        "model": model,
        "is_batch": is_batch,
        "token_usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens},
        # SerpWow is a flat fee (no per-search USD charge), so total_usd == llm_usd by design.
        "cost": {"llm_usd": round(llm_usd, 6), "serpwow_searches": serpwow_searches,
                 "total_usd": round(llm_usd, 6)},
        "processing_seconds_total": state.get("processing_seconds_total"),
    }


def _csv_row(r: EntityResult) -> dict[str, Any]:
    return {"company_name": r.company_name, "company_local_name": r.company_local_name or "",
            "country": r.country, "website_url": r.website_url or "",
            "confidence": r.confidence, "flags": r.flags_csv(),
            "attempt_log": r.attempt_log_csv()}


def write_gsearch_outputs(upload_dir: Path, state: dict[str, Any]) -> dict[str, Path]:
    upload_dir = Path(upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    results = state_to_entity_results(state)
    summary = build_summary(state, results)

    paths: dict[str, Path] = {}
    for name, rows, extra in (("found.csv", [r for r in results if r.website_url], []),
                              ("notFound.csv", [r for r in results if not r.website_url], ["error"])):
        path = upload_dir / name
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS + extra)
            writer.writeheader()
            for r in rows:
                row = _csv_row(r)
                if extra:
                    row["error"] = r.error or ""
                writer.writerow(row)
        paths[name] = path

    report = {"summary": summary, "rows": [r.to_report_dict() for r in results]}
    report_path = upload_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["report.json"] = report_path

    log_lines: list[str] = []
    for r in results:
        if r.website_url:
            log_lines.append(f"[{r.sno}] {r.company_name} ({r.country}) -> "
                             f"{r.website_url} (confidence={r.confidence})")
        else:
            tail = f" — {r.error}" if r.error else ""
            log_lines.append(f"[{r.sno}] {r.company_name} ({r.country}) -> not found{tail}")
    log_path = upload_dir / "run.log"
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    paths["run.log"] = log_path

    return paths
