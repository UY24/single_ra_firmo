# backend/app/services/serpwow/serpwow_reporting.py
"""found.csv / notFound.csv / report.json / run.log for SerpWow pipelines.

Pipeline-agnostic: converts a terminal SerpWow upload ``state["rows"]`` (each row
carries a CrawlResponse dict under ``result``) into the shared EntityResult
schema, then writes the same file set AI Mode produces so the Runs UI can view
them uniformly. Used by both ``gsearch`` (LLM confidence) and ``gmaps``
(heuristic confidence) — the confidence block is read from whichever context key
the pipeline populated.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

from app.models.results import AttemptLogEntry, EntityResult, Flag

CSV_COLUMNS = ["company_name", "company_local_name", "country", "website_url",
               "confidence", "flags", "attempt_log"]

REL_OUTPUT_COLUMNS = ["website_url", "relationship_status", "relationship_summary",
                      "confidence", "flags", "attempt_log", "verified_pair"]


def _confidence_raw(result: dict[str, Any]) -> dict[str, Any]:
    ctx = result.get("context") or {}
    for key in ("final_url_selection_ai", "gemini_batch_ai", "gmaps_confidence"):
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
    if raw.get("organizational_mismatch"):
        flags.append(Flag("organizational_mismatch",
                          "listing may be a different organization type — verify"))
    if raw.get("address_conflict"):
        flags.append(Flag("address_conflict",
                          "listing address may conflict with the input — verify"))
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
        error_source=row.get("error_source"),
        error_category=row.get("error_category"),
        degraded_search=bool(row.get("degraded_search")),
    )


def _relationship_block(result: dict[str, Any]) -> dict[str, Any]:
    ctx = result.get("context") or {}
    rel = ctx.get("relationship")
    return rel if isinstance(rel, dict) else {}


def _relationship_pair_to_entity_result(row: dict[str, Any], sno: int) -> EntityResult:
    """One pair row -> one EntityResult (relationship flags folded in).
    Fan-out to original rows happens in state_to_entity_results."""
    base = row_to_entity_result(row, sno)
    rel = _relationship_block(row.get("result") or {})
    status = str(rel.get("status") or "")
    if status:
        base.flags.insert(0, Flag("relationship_status", status))
    summary = str(rel.get("summary") or "")
    if summary:
        base.flags.append(Flag("relationship_summary", summary))
    for f in rel.get("flags") or []:
        if isinstance(f, dict) and f.get("flag"):
            base.flags.append(Flag(str(f["flag"]), str(f.get("why") or "")))
    return base


def _relationship_expanded(state: dict[str, Any]) -> list[tuple[EntityResult, dict[str, Any], dict[str, Any]]]:
    """[(entity_result_copy, original_row_dict, pair_row)] — one entry per
    SEARCHABLE original CSV row, ordered by original row index."""
    import copy
    meta = state.get("relationship") or {}
    original_rows = meta.get("original_rows") or []
    expanded: list[tuple[int, EntityResult, dict[str, Any], dict[str, Any]]] = []
    for pair_row in state.get("rows", []):
        if not isinstance(pair_row, dict):
            continue
        base = _relationship_pair_to_entity_result(pair_row, 0)
        for source_idx in pair_row.get("source_row_indices") or []:
            idx = int(source_idx)
            original = original_rows[idx] if 0 <= idx < len(original_rows) else {}
            expanded.append((idx, copy.deepcopy(base), original, pair_row))
    expanded.sort(key=lambda item: item[0])
    out = []
    for sno, (idx, er, original, pair_row) in enumerate(expanded, start=1):
        er.sno = sno
        out.append((er, original, pair_row))
    return out


def state_to_entity_results(state: dict[str, Any]) -> list[EntityResult]:
    if str(state.get("pipeline") or "") == "relationship":
        return [er for er, _original, _pair in _relationship_expanded(state)]
    return [row_to_entity_result(r, i + 1) for i, r in enumerate(state.get("rows", []))]


def _build_cost(llm_usd: float, serpwow_searches: int) -> dict[str, Any]:
    try:
        rate = float(os.getenv("SERPWOW_USD_PER_SEARCH", "") or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    serpwow_usd = serpwow_searches * rate
    return {
        "llm_usd": round(llm_usd, 6),
        "serpwow_searches": serpwow_searches,
        "serpwow_usd": round(serpwow_usd, 6),
        "total_usd": round(llm_usd + serpwow_usd, 6),
    }


def _derive_outcome(row: dict[str, Any]) -> Any:
    """Outcome for one row, mirroring engine.summarize_upload_state._outcome_of so the
    report.json / Supabase / Slack breakdown reconciles with the state summary:
    explicit ``outcome`` wins; else completed -> found if official_website else not_found;
    else failed -> error (covers user-stop / redelivery-drop / stale rows that carry no
    explicit outcome); else uncounted."""
    oc = row.get("outcome")
    if oc:
        return oc
    if row.get("status") == "completed":
        result_obj = row.get("result") if isinstance(row.get("result"), dict) else {}
        return "found" if result_obj.get("official_website") else "not_found"
    if row.get("status") == "failed":
        return "error"
    return None


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
    # SerpWow gsearch is per-request billed (unlike scrape.do's flat fee), so
    # surface a USD figure. Rate unset -> 0 (no crash).
    summary = {
        "upload_id": state.get("upload_id"),
        "company_name": state.get("company_name"),
        "pipeline": state.get("pipeline"),
        "status": state.get("status"),
        "total_rows": len(results),
        "websites_found": found,
        "websites_not_found": len(results) - found,
        "model": model,
        # "llm" when a confidence model actually ran (gsearch always; gmaps only when
        # GMAPS_CONFIDENCE_MODE=llm), else "heuristic" (gmaps default). Lets the UI show
        # the confidence chip without guessing from model presence. relationship is
        # ALWAYS LLM by design (the gate needs it), even mid-run before a model ran.
        "confidence_mode": "llm" if (model or str(state.get("pipeline") or "") == "relationship") else "heuristic",
        "is_batch": is_batch,
        "token_usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens},
        "cost": _build_cost(llm_usd, serpwow_searches),
        "processing_seconds_total": state.get("processing_seconds_total"),
    }
    # Outcome/error breakdown, aggregated straight off state["rows"] by each row's
    # outcome/error_source/error_category. For relationship this is pair-level (one
    # entry per unique X,Y pair row), not fanned out to original CSV rows — the
    # breakdown is a diagnostic, so pair-level is acceptable and simpler.
    outcome_breakdown = {"found": 0, "not_found": 0, "errored": 0}
    by_source: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for row in state.get("rows", []):
        oc = _derive_outcome(row or {})
        if oc == "found":
            outcome_breakdown["found"] += 1
        elif oc == "not_found":
            outcome_breakdown["not_found"] += 1
        elif oc == "error":
            outcome_breakdown["errored"] += 1
            if row.get("error_source"):
                by_source[row["error_source"]] = by_source.get(row["error_source"], 0) + 1
            if row.get("error_category"):
                by_category[row["error_category"]] = by_category.get(row["error_category"], 0) + 1
    summary["outcome_breakdown"] = outcome_breakdown
    summary["error_breakdown"] = {"by_source": by_source, "by_category": by_category}

    meta = state.get("relationship") if isinstance(state.get("relationship"), dict) else None
    if meta is not None:
        breakdown = {"confirmed": 0, "not_confirmed": 0, "unclear": 0}
        for pair_row in state.get("rows", []):
            rel = _relationship_block((pair_row or {}).get("result") or {})
            status = str(rel.get("status") or "")
            n_sources = len((pair_row or {}).get("source_row_indices") or [])
            if status in breakdown:
                breakdown[status] += n_sources
        summary["total_rows"] = int(meta.get("row_count_original") or 0)
        summary["blank_rows"] = int(meta.get("blank_rows") or 0)
        summary["searchable_rows"] = summary["total_rows"] - summary["blank_rows"]
        summary["unique_pairs"] = len(state.get("rows", []))
        summary["relationship_breakdown"] = breakdown
    return summary


def _csv_row(r: EntityResult) -> dict[str, Any]:
    return {"company_name": r.company_name, "company_local_name": r.company_local_name or "",
            "country": r.country, "website_url": r.website_url or "",
            "confidence": r.confidence, "flags": r.flags_csv(),
            "attempt_log": r.attempt_log_csv()}


def write_outputs(upload_dir: Path, state: dict[str, Any]) -> dict[str, Path]:
    if str(state.get("pipeline") or "") == "relationship":
        return _write_relationship_outputs(Path(upload_dir), state)
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

    summary_hdr = [
        f"# {summary.get('pipeline')} run {summary.get('upload_id')} — status={summary.get('status')}",
        f"# rows={summary.get('total_rows')} found={summary.get('websites_found')} "
        f"not_found={summary.get('websites_not_found')} batch={summary.get('is_batch')} "
        f"model={summary.get('model')}",
        f"# cost: llm_usd={summary['cost']['llm_usd']} serpwow_usd={summary['cost']['serpwow_usd']} "
        f"total_usd={summary['cost']['total_usd']} serpwow_searches={summary['cost']['serpwow_searches']}",
        "",
    ]
    log_path = upload_dir / "run.log"
    log_path.write_text("\n".join(summary_hdr + log_lines) + "\n", encoding="utf-8")
    paths["run.log"] = log_path

    return paths


def _write_relationship_outputs(upload_dir: Path, state: dict[str, Any]) -> dict[str, Path]:
    upload_dir.mkdir(parents=True, exist_ok=True)
    meta = state.get("relationship") or {}
    header = [h for h in (meta.get("header") or []) if h]
    original_rows = meta.get("original_rows") or []
    expanded = _relationship_expanded(state)
    results = [er for er, _o, _p in expanded]
    summary = build_summary(state, results)
    paths: dict[str, Path] = {}

    def _out_row(er: EntityResult, original: dict[str, Any], pair_row: dict[str, Any]) -> dict[str, Any]:
        rel = _relationship_block((pair_row or {}).get("result") or {})
        row = {h: str(original.get(h, "") or "") for h in header}
        row.update({
            "website_url": er.website_url or "",
            "relationship_status": str(rel.get("status") or ""),
            "relationship_summary": str(rel.get("summary") or ""),
            "confidence": er.confidence,
            "flags": er.flags_csv(),
            "attempt_log": er.attempt_log_csv(),
            "verified_pair": str(rel.get("verified_pair") or ""),
        })
        return row

    for name, keep, extra in (
        ("found.csv", lambda er: bool(er.website_url), []),
        ("notFound.csv", lambda er: not er.website_url, ["error"]),
    ):
        path = upload_dir / name
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=header + REL_OUTPUT_COLUMNS + extra)
            writer.writeheader()
            for er, original, pair_row in expanded:
                if not keep(er):
                    continue
                row = _out_row(er, original, pair_row)
                if extra:
                    row["error"] = er.error or ""
                writer.writerow(row)
        paths[name] = path

    skipped_path = upload_dir / "skipped.csv"
    with skipped_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header + ["skip_reason"])
        writer.writeheader()
        for idx in meta.get("blank_row_indices") or []:
            i = int(idx)
            original = original_rows[i] if 0 <= i < len(original_rows) else {}
            row = {h: str(original.get(h, "") or "") for h in header}
            row["skip_reason"] = "blank_company_name_y"
            writer.writerow(row)
    paths["skipped.csv"] = skipped_path

    report_rows = []
    for er, _original, pair_row in expanded:
        rel = _relationship_block((pair_row or {}).get("result") or {})
        d = er.to_report_dict()
        d["relationship_status"] = str(rel.get("status") or "")
        d["relationship_summary"] = str(rel.get("summary") or "")
        d["verified_pair"] = str(rel.get("verified_pair") or "")
        report_rows.append(d)
    report_path = upload_dir / "report.json"
    report_path.write_text(json.dumps({"summary": summary, "rows": report_rows},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
    paths["report.json"] = report_path

    log_lines = []
    for er, _original, pair_row in expanded:
        rel = _relationship_block((pair_row or {}).get("result") or {})
        if er.website_url:
            log_lines.append(f"[{er.sno}] {er.company_name} ({rel.get('verified_pair')}) -> "
                             f"{er.website_url} (confidence={er.confidence}, "
                             f"relationship={rel.get('status')})")
        else:
            tail = f" — {er.error}" if er.error else ""
            log_lines.append(f"[{er.sno}] {er.company_name} ({rel.get('verified_pair')}) -> "
                             f"not found (relationship={rel.get('status')}){tail}")
    hdr = [
        f"# relationship run {summary.get('upload_id')} — status={summary.get('status')}",
        f"# original_rows={summary.get('total_rows')} blank={summary.get('blank_rows')} "
        f"pairs={summary.get('unique_pairs')} found={summary.get('websites_found')} "
        f"not_found={summary.get('websites_not_found')}",
        f"# relationship: {json.dumps(summary.get('relationship_breakdown'))}",
        f"# cost: llm_usd={summary['cost']['llm_usd']} serpwow_usd={summary['cost']['serpwow_usd']} "
        f"total_usd={summary['cost']['total_usd']} serpwow_searches={summary['cost']['serpwow_searches']}",
        "",
    ]
    log_path = upload_dir / "run.log"
    log_path.write_text("\n".join(hdr + log_lines) + "\n", encoding="utf-8")
    paths["run.log"] = log_path
    return paths
