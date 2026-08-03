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
from typing import Any, Optional

from app.models.results import AttemptLogEntry, EntityResult, Flag
from app.services.serpwow.serpwow_client import sanitize_serpwow_error_text

CSV_COLUMNS = ["company_name", "company_local_name", "country", "website_url",
               "confidence", "flags", "attempt_log"]

REL_OUTPUT_COLUMNS = ["website_url", "resolved_company_y_name",
                      "relationship_status", "relationship_summary",
                      "relationship_evidence", "relationship_confidence",
                      "website_confidence", "confidence", "phases_used",
                      "flags", "attempt_log", "error_source", "error_reason"]


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
    # A per-row Gemini selection failure that fell back to the raw candidate: the row
    # is still found (degraded), but make the failure visible in the report.
    llm_error = ctx.get("llm_error")
    if llm_error:
        flags.append(Flag("llm_selection_failed", str(llm_error)))

    attempts: list[AttemptLogEntry] = []
    for fr in ctx.get("formatted_results") or []:
        if not isinstance(fr, dict):
            continue
        outcome = sanitize_serpwow_error_text(
            fr.get("result") or
            ("ok" if fr.get("success") else (fr.get("error") or "no result")))
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
        error=(sanitize_serpwow_error_text(row.get("error"))
               if row.get("error") else None),
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


def _build_cost(llm_usd: float, serpwow_searches: int, billable_searches: int,
                scrapedo_requests: int = 0, scrapedo_credits: int = 0,
                scrapedo_successful_requests: int = 0,
                scrapedo_failed_requests: int = 0,
                scrapedo_billed_empty: int = 0) -> dict[str, Any]:
    """SerpWow is per-search USD; scrape.do is credits (10 per successful call) with no
    USD figure. Both key sets are always present so a run whose pipeline has migrated
    and a pre-migration run of the same pipeline each render from their own fields.

    scrape.do call accounting reconciles as
    ``scrapedo_requests == scrapedo_successful_requests + scrapedo_failed_requests``,
    with credits charged only on the successful ones.
    """
    try:
        rate = float(os.getenv("SERPWOW_USD_PER_SEARCH", "") or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    serpwow_usd = billable_searches * rate
    return {
        "llm_usd": round(llm_usd, 6),
        "serpwow_searches": serpwow_searches,
        "serpwow_billable_searches": billable_searches,
        "serpwow_usd": round(serpwow_usd, 6),
        "scrapedo_requests": scrapedo_requests,
        "scrapedo_successful_requests": scrapedo_successful_requests,
        "scrapedo_failed_requests": scrapedo_failed_requests,
        # Billed HTTP 200s that returned zero results: credits spent for no data, i.e.
        # the refund claim to raise with scrape.do. Not the same as a free 502.
        "scrapedo_billed_empty": scrapedo_billed_empty,
        "scrapedo_credits": scrapedo_credits,
        "total_usd": round(llm_usd + serpwow_usd, 6),
    }


def _cost_log_line(summary: dict[str, Any]) -> str:
    """The run.log ``# cost:`` header. Credits appear only for scrape.do-billed runs,
    so a SerpWow run's line is unchanged."""
    cost = summary.get("cost") or {}
    line = (f"# cost: llm_usd={cost.get('llm_usd')} serpwow_usd={cost.get('serpwow_usd')} "
            f"total_usd={cost.get('total_usd')} serpwow_searches={cost.get('serpwow_searches')}")
    if cost.get("scrapedo_requests") or cost.get("scrapedo_credits"):
        line += (f" scrapedo_requests={cost.get('scrapedo_requests')}"
                 f" (ok={cost.get('scrapedo_successful_requests')}"
                 f" failed={cost.get('scrapedo_failed_requests')})"
                 f" scrapedo_credits={cost.get('scrapedo_credits')}")
        if cost.get("scrapedo_billed_empty"):
            line += f" scrapedo_billed_empty={cost.get('scrapedo_billed_empty')}"

    return line


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


def _phase_is_empty(item: dict[str, Any]) -> Optional[bool]:
    """True if a phase returned an "empty 200" (no AI overview + 0 candidates).

    None when the phase errored (a transport/HTTP failure is a different problem,
    not an empty response) or the fields are missing (older pre-feature runs).
    """
    if not isinstance(item, dict) or item.get("error"):
        return None
    if "ai_overview_present" not in item and "candidate_count" not in item:
        return None
    return (not item.get("ai_overview_present")) and int(item.get("candidate_count") or 0) == 0


def empty_response_breakdown(state: dict[str, Any]) -> Optional[dict[str, int]]:
    """Count rows whose SerpWow phases came back empty despite HTTP 200.

    relationship (exactly 2 phases): both_phases / phase1_only / phase2_only.
    gsearch (variable phase count): all_phases / some_phases.
    Other pipelines: None (they don't run AI-overview searches).
    """
    pipeline = str(state.get("pipeline") or "")
    is_rel = pipeline == "relationship"
    if not (is_rel or pipeline == "gsearch"):
        return None
    out = ({"both_phases": 0, "phase1_only": 0, "phase2_only": 0} if is_rel
           else {"all_phases": 0, "some_phases": 0})
    for row in state.get("rows", []):
        ctx = ((row or {}).get("result") or {}).get("context") or {}
        phases = ctx.get("formatted_results")
        if not isinstance(phases, list) or not phases:
            continue
        flags = [_phase_is_empty(p) for p in phases]
        considered = [f for f in flags if f is not None]
        if not considered:
            continue
        # relationship rows are pairs fanned out to original CSV rows.
        weight = len((row or {}).get("source_row_indices") or []) if is_rel else 1
        if is_rel:
            e1 = flags[0] if len(flags) > 0 else None
            e2 = flags[1] if len(flags) > 1 else None
            if e1 and e2:
                out["both_phases"] += weight
            elif e1:
                out["phase1_only"] += weight
            elif e2:
                out["phase2_only"] += weight
        else:
            n_empty = sum(1 for f in considered if f)
            if n_empty == len(considered):
                out["all_phases"] += weight
            elif n_empty:
                out["some_phases"] += weight
    return out


def build_summary(state: dict[str, Any], results: list[EntityResult]) -> dict[str, Any]:
    found = sum(1 for r in results if r.website_url)
    serpwow_searches = 0
    billable_searches = 0
    scrapedo_requests = 0
    scrapedo_credits = 0
    scrapedo_ok = 0
    scrapedo_failed = 0
    scrapedo_billed_empty = 0
    llm_usd = 0.0
    prompt_tokens = 0
    completion_tokens = 0
    model: str | None = None
    for row in state.get("rows", []):
        result = row.get("result") or {}
        ctx = result.get("context") or {}
        cb = ctx.get("cost_breakdown") or {}
        # A row is billed by exactly ONE provider. scrape.do rows (gmaps) carry no
        # SerpWow keys at all, so they must skip SerpWow accounting entirely —
        # otherwise the billable-count fallback below would infer 1 billable search
        # from their formatted_results entry and price them at SERPWOW_USD_PER_SEARCH.
        # A pre-migration gmaps run has no scrapedo_* keys and still lands in the
        # SerpWow branch, which is what keeps its old cost card rendering.
        if "scrapedo_requests" in cb or "scrapedo_credits" in cb:
            scrapedo_requests += int(cb.get("scrapedo_requests") or 0)
            scrapedo_credits += int(cb.get("scrapedo_credits") or 0)
            scrapedo_ok += int(cb.get("scrapedo_successful_requests") or 0)
            scrapedo_failed += int(cb.get("scrapedo_failed_requests") or 0)
            scrapedo_billed_empty += int(cb.get("scrapedo_billed_empty") or 0)
        else:
            request_count = int(cb.get("serpwow_request_count") or 0)
            serpwow_searches += request_count
            if "serpwow_billable_request_count" in cb:
                billable_searches += int(cb.get("serpwow_billable_request_count") or 0)
            else:
                formatted = ctx.get("formatted_results")
                billable_searches += (
                    sum(1 for item in formatted
                        if isinstance(item, dict) and item.get("success"))
                    if isinstance(formatted, list) and formatted else request_count
                )
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
        "cost": _build_cost(llm_usd, serpwow_searches, billable_searches,
                            scrapedo_requests, scrapedo_credits,
                            scrapedo_ok, scrapedo_failed, scrapedo_billed_empty),
        "processing_seconds_total": state.get("processing_seconds_total"),
    }
    # Outcome/error breakdown is original-row-level. Relationship state rows are
    # deduplicated pairs, so each outcome must fan out by source_row_indices to
    # reconcile with searchable_rows and the generated CSVs. Blank rows never enter
    # state["rows"]. Non-relationship state rows each represent one input row.
    outcome_breakdown = {"found": 0, "not_found": 0, "errored": 0}
    by_source: dict[str, int] = {}
    by_category: dict[str, int] = {}
    is_relationship = str(state.get("pipeline") or "") == "relationship"
    for row in state.get("rows", []):
        weight = len((row or {}).get("source_row_indices") or []) if is_relationship else 1
        oc = _derive_outcome(row or {})
        if oc == "found":
            outcome_breakdown["found"] += weight
        elif oc == "not_found":
            outcome_breakdown["not_found"] += weight
        elif oc == "error":
            outcome_breakdown["errored"] += weight
            if weight and row.get("error_source"):
                by_source[row["error_source"]] = by_source.get(row["error_source"], 0) + weight
            if weight and row.get("error_category"):
                by_category[row["error_category"]] = by_category.get(row["error_category"], 0) + weight
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
        summary["relationship_breakdown"] = breakdown

    ebd = empty_response_breakdown(state)
    if ebd is not None:
        summary["empty_response_breakdown"] = ebd
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
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
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
        _cost_log_line(summary),
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
    expanded = _relationship_expanded(state)
    results = [er for er, _o, _p in expanded]
    summary = build_summary(state, results)
    paths: dict[str, Path] = {}

    def _out_row(er: EntityResult, original: dict[str, Any], pair_row: dict[str, Any]) -> dict[str, Any]:
        result = (pair_row or {}).get("result") or {}
        rel = _relationship_block(result)
        raw = _confidence_raw(result)
        evidence = rel.get("evidence") or raw.get("relationship_evidence") or []
        context = result.get("context") if isinstance(result.get("context"), dict) else {}
        row = {h: str(original.get(h, "") or "") for h in header}
        row.update({
            "website_url": er.website_url or "",
            "resolved_company_y_name": str(
                rel.get("resolved_company_y_name")
                or raw.get("resolved_company_y_name") or ""),
            "relationship_status": str(rel.get("status") or ""),
            "relationship_summary": str(rel.get("summary") or ""),
            "relationship_evidence": "\n".join(str(item) for item in evidence if str(item).strip()),
            "relationship_confidence": int(
                rel.get("relationship_confidence_score")
                or raw.get("relationship_confidence_score") or 0),
            "website_confidence": int(
                rel.get("website_confidence_score")
                or raw.get("website_confidence_score") or 0),
            "confidence": er.confidence,
            "phases_used": len(context.get("formatted_results") or []),
            "flags": er.flags_csv(),
            "attempt_log": er.attempt_log_csv(),
            "error_source": er.error_source or "",
            "error_reason": er.error or "",
        })
        return row

    # Split by RELATIONSHIP STATUS (not URL presence): confirmed vs everything else
    # (not_confirmed + unclear, plus any error/pending row → caught by the != branch,
    # so no row is ever dropped). website_url stays in the row so you can see which
    # confirmed rows also resolved a URL; the found/not-found URL counts live in the
    # report.json summary (websites_found / websites_not_found).
    def _status_of(pair_row: dict[str, Any]) -> str:
        return str(_relationship_block((pair_row or {}).get("result") or {}).get("status") or "")

    for name, keep in (
        ("confirmed_relation.csv", lambda s: s == "confirmed"),
        ("notconfirmed_relation.csv", lambda s: s != "confirmed"),
    ):
        path = upload_dir / name
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=header + REL_OUTPUT_COLUMNS)
            writer.writeheader()
            for er, original, pair_row in expanded:
                if not keep(_status_of(pair_row)):
                    continue
                writer.writerow(_out_row(er, original, pair_row))
        paths[name] = path

    report_rows = []
    for er, _original, pair_row in expanded:
        rel = _relationship_block((pair_row or {}).get("result") or {})
        d = er.to_report_dict()
        d["relationship_status"] = str(rel.get("status") or "")
        d["relationship_summary"] = str(rel.get("summary") or "")
        d["resolved_company_y_name"] = str(rel.get("resolved_company_y_name") or "")
        d["relationship_evidence"] = list(rel.get("evidence") or [])
        d["relationship_confidence"] = int(
            rel.get("relationship_confidence_score") or 0)
        d["website_confidence"] = int(rel.get("website_confidence_score") or 0)
        result = (pair_row or {}).get("result") or {}
        ctx = result.get("context") if isinstance(result.get("context"), dict) else {}
        d["phases_used"] = len(ctx.get("formatted_results") or [])
        d["error_reason"] = er.error or ""
        report_rows.append(d)
    report_path = upload_dir / "report.json"
    report_path.write_text(json.dumps({"summary": summary, "rows": report_rows},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
    paths["report.json"] = report_path

    log_lines = []
    for er, _original, pair_row in expanded:
        rel = _relationship_block((pair_row or {}).get("result") or {})
        source = f", error_source={er.error_source}" if er.error_source else ""
        if er.website_url:
            log_lines.append(f"[{er.sno}] {er.company_name} -> "
                             f"{er.website_url} (confidence={er.confidence}, "
                             f"relationship={rel.get('status')}{source})")
        else:
            tail = f" — {er.error}" if er.error else ""
            log_lines.append(f"[{er.sno}] {er.company_name} -> "
                             f"not found (relationship={rel.get('status')}{source}){tail}")
    hdr = [
        f"# relationship run {summary.get('upload_id')} — status={summary.get('status')}",
        f"# rows={summary.get('total_rows')} found={summary.get('websites_found')} "
        f"not_found={summary.get('websites_not_found')}",
        f"# relationship: {json.dumps(summary.get('relationship_breakdown'))}",
        _cost_log_line(summary),
        "",
    ]
    log_path = upload_dir / "run.log"
    log_path.write_text("\n".join(hdr + log_lines) + "\n", encoding="utf-8")
    paths["run.log"] = log_path
    return paths
