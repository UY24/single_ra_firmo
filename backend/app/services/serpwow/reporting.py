# backend/app/services/serpwow/reporting.py
"""found.csv / notFound.csv / report.json / run.log + the run summary, for the
state-driven upload pipelines.

**Provider-agnostic** — it was ``serpwow_reporting.py`` until 2026-08-19, by which point
three of the four pipelines it serves had migrated to scrape.do and the name was simply
wrong. It converts a terminal upload's ``state["rows"]`` (each row carrying a
CrawlResponse dict under ``result``) into the shared EntityResult schema, then writes the
same file set AI Mode produces so the Runs UI can view them uniformly.

Consumers: ``gsearch`` (SerpWow, LLM confidence), ``relationship`` and ``firmographics``
(scrape.do) for their summaries, and ``gmaps_outputs``/``relationship_outputs`` for the
shared per-row conversion. Cost accounting routes on the DATA — the presence of
``scrapedo_*`` keys in a row's ``cost_breakdown`` — not on the pipeline name, which is why
adding a migrated pipeline needed no branch here.

Deliberately standalone-ish: it imports only ``models/results.py`` plus siblings, to avoid
a circular import back into ``engine``.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any, Iterable, Optional

from app.models.results import AttemptLogEntry, EntityResult, Flag
from app.services.common.text import passthrough_fieldnames, passthrough_row
from app.services.serpwow.serpwow_client import sanitize_serpwow_error_text

# What we WORKED OUT for a row. The pipelines that can reach the uploaded input.csv
# (gmaps, relationship) put the user's own columns in front of these; gsearch cannot —
# it never persists the upload — so it keeps echoing the three parsed fields below and
# CSV_COLUMNS stays exactly what it has always been.
RESULT_COLUMNS = ["website_url", "confidence", "flags", "attempt_log"]
CSV_COLUMNS = ["company_name", "company_local_name", "country"] + RESULT_COLUMNS

# retry.csv's own column, added after the input header. Deduped by retry_column() when an
# input CSV already has one by that name.
RETRY_REASON_COLUMN = "retry_reason"

# iter_input_rows overwrites a real "row_index" input column with its own int index and
# parks the original value here — so passing an input row back out has to read from here.
_INPUT_SOURCE_OVERRIDES = {"row_index": "row_index__input"}


def s3_passthrough(header: list[str], reserved: Iterable[str]) -> list[tuple[str, str]]:
    """`passthrough_fieldnames` for the S3-only pipelines, whose rows come from
    `s3_run_store.iter_input_rows` and therefore carry its injected `row_index`."""
    return passthrough_fieldnames(header, reserved, _INPUT_SOURCE_OVERRIDES)


def result_cells(entity: EntityResult) -> dict[str, Any]:
    """The RESULT_COLUMNS four, for one row."""
    return {"website_url": entity.website_url or "", "confidence": entity.confidence,
            "flags": entity.flags_csv(), "attempt_log": entity.attempt_log_csv()}


def retry_column(header: list[str]) -> str:
    """The reason column's name, suffixed until it cannot collide with an input column."""
    name = RETRY_REASON_COLUMN
    while name in header:
        name += "_"
    return name


def retry_row(original: dict[str, Any], header: list[str], reason_column: str, *,
              attempts: int, credits: int, error: str = "",
              billed_empty: bool = False, no_listing: bool = False,
              llm_incomplete: bool = False) -> Optional[dict[str, str]]:
    """One retry.csv row, or None when the row got a real answer.

    retry.csv exists to be RE-UPLOADED, so it carries the original input cells verbatim
    under the original header and adds exactly one column. Only rows with nothing to show
    for themselves belong in it — a row the provider answered is finished, and running it
    again just buys the same answer twice:

    - ``billed_empty`` — HTTP 200 that carried no data. The only case where credits were
      charged for nothing, i.e. the scrape.do refund claim. Listed first because it is the
      only reason that costs money.
    - ``no_listing`` — every attempt came back 502 "no results" (gmaps). Not billed.
    - ``error``      — died after every retry, or never ran at all.
    - ``llm_incomplete`` — the provider answered and we were billed, but the Gemini shard
      that had to read that answer never delivered (job FAILED/EXPIRED, or our poll gave
      up). The row has a scrape and no verdict. Listed LAST because it is the only reason
      whose rerun costs nothing from the provider: the scrape objects already exist, so a
      re-drive skips the row and redoes the LLM half alone.

    Shared by gmaps, relationship and firmographics so one vocabulary describes every
    run's rerun list.
    """
    if billed_empty:
        reason = "billed_empty: HTTP 200 returned no data — refundable"
    elif no_listing:
        reason = 'no_listing: 502 "no results" after every retry — not billed'
    elif error:
        # Credits on an errored row mean the call returned HTTP 200 and the BODY carried
        # the error — charged for nothing, exactly like billed_empty. A row that died
        # before any 200 cost nothing, so it must not claim to be refundable.
        reason = f"error: {error}" + (" — billed, refundable" if credits else "")
    elif llm_incomplete:
        reason = ("llm_incomplete: scraped and billed, but the Gemini batch never returned "
                  "a result for this row — rerun redoes the LLM only, no provider re-spend")
    else:
        return None
    row = passthrough_row(original, s3_passthrough(header, ()))
    row[reason_column] = f"{reason} | attempts={attempts} credits={credits}"
    return row


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


def state_to_entity_results(state: dict[str, Any]) -> list[EntityResult]:
    return [row_to_entity_result(r, i + 1) for i, r in enumerate(state.get("rows", []))]


def _build_cost(llm_usd: float, serpwow_searches: int, billable_searches: int,
                scrapedo_requests: int = 0, scrapedo_credits: int = 0,
                scrapedo_successful_requests: int = 0,
                scrapedo_failed_requests: int = 0,
                scrapedo_billed_empty: int = 0,
                scrapedo_no_results: int = 0,
                scrapedo_recovered_requests: int = 0,
                scrapedo_error_requests: int = 0,
                scrapedo_billed_errors: int = 0,
                scrapedo_search_requests: int = 0,
                scrapedo_search_successful: int = 0,
                scrapedo_ai_overview_requests: int = 0,
                scrapedo_ai_overview_successful: int = 0,
                scrapedo_ai_overview_deferred: int = 0) -> dict[str, Any]:
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
        # Non-200s that are NOT errors: Google simply has no Maps listing. Unbilled,
        # not retried, and reported apart from real failures so a clean run does not
        # look like it had 12 failures.
        "scrapedo_no_results": scrapedo_no_results,
        # Failed attempts split by what the ROW became: a 502 that recovered on retry
        # is not an error, only a row that failed after every retry is.
        "scrapedo_recovered_requests": scrapedo_recovered_requests,
        "scrapedo_error_requests": scrapedo_error_requests,
        "scrapedo_billed_empty": scrapedo_billed_empty,
        # Rows whose HTTP 200 came back with an ERROR body. Billed all the same, so they
        # sit with billed_empty as "credits spent for nothing", not with the free 502s.
        "scrapedo_billed_errors": scrapedo_billed_errors,
        # Per-ENDPOINT split, because scrape.do prices them differently: a Google Search
        # 200 is 10 credits, a deferred AI-Overview follow-up is 5. Without this the
        # firmographics bill (rows x 10, plus 5 for each deferred row) is unexplainable
        # from the totals alone. Zero for every other pipeline.
        "scrapedo_search_requests": scrapedo_search_requests,
        "scrapedo_search_successful": scrapedo_search_successful,
        "scrapedo_ai_overview_requests": scrapedo_ai_overview_requests,
        "scrapedo_ai_overview_successful": scrapedo_ai_overview_successful,
        # Rows where Google deferred the overview, i.e. that needed the 5-credit call.
        "scrapedo_ai_overview_deferred": scrapedo_ai_overview_deferred,
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
        failed = int(cost.get("scrapedo_failed_requests") or 0)
        recovered = int(cost.get("scrapedo_recovered_requests") or 0)
        errors = int(cost.get("scrapedo_error_requests") or 0)
        # Whatever is left failed on a row that ended "no Google listing" — expected.
        no_listing_calls = max(0, failed - recovered - errors)
        line += (f" scrapedo_requests={cost.get('scrapedo_requests')}"
                 f" (ok={cost.get('scrapedo_successful_requests')}"
                 f" recovered={recovered} no_listing={no_listing_calls}"
                 f" errors={errors})"
                 f" scrapedo_credits={cost.get('scrapedo_credits')}"
                 f" rows_no_listing={cost.get('scrapedo_no_results')}")
        if cost.get("scrapedo_billed_empty"):
            line += f" scrapedo_billed_empty={cost.get('scrapedo_billed_empty')}"
        if cost.get("scrapedo_billed_errors"):
            line += f" scrapedo_billed_errors={cost.get('scrapedo_billed_errors')}"

    return line


# Fields a firmographics row must have gained for the row to count as "found". Its
# official_website is the INPUT echoed back, so unlike every other pipeline it proves
# nothing — see row_produced_a_result.
FIRMOGRAPHICS_OUTPUT_FIELDS = ("address", "phone", "email", "industry",
                               "products", "services")


def row_produced_a_result(result: dict[str, Any], pipeline: str) -> bool:
    """Did this row actually produce an answer? The fallback for rows with no explicit
    ``outcome`` (legacy rows, user-stop, redelivery-drop).

    Shared by ``_derive_outcome`` here and ``engine.summarize_upload_state._outcome_of``,
    which must agree or report.json and /status disagree about the same run. Split out
    because firmographics needs the opposite answer from everyone else: it is HANDED the
    website, so ``official_website`` being set is just the input coming back.
    """
    result = result if isinstance(result, dict) else {}
    if str(pipeline or "") == "firmographics":
        return any(result.get(field) for field in FIRMOGRAPHICS_OUTPUT_FIELDS)
    return bool(result.get("official_website"))


def _derive_outcome(row: dict[str, Any], pipeline: str = "") -> Any:
    """Outcome for one row, mirroring engine.summarize_upload_state._outcome_of so the
    report.json / Supabase / Slack breakdown reconciles with the state summary:
    explicit ``outcome`` wins; else completed -> found if the row produced a result else
    not_found; else failed -> error (covers user-stop / redelivery-drop / stale rows that
    carry no explicit outcome); else uncounted."""
    oc = row.get("outcome")
    if oc:
        return oc
    if row.get("status") == "completed":
        return ("found" if row_produced_a_result(row.get("result"), pipeline)
                else "not_found")
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
    """Per-pipeline count of rows the provider answered with nothing usable.

    relationship moved to scrape.do in 2026-08 and reports its own
    {"empty": N} from relationship_outputs, so it no longer routes through here.
    """
    pipeline = str(state.get("pipeline") or "")
    if pipeline == "firmographics":
        # firmographics buys ONE search per row and extracts from ai_overview alone, so
        # the only "answered but useless" case is a billed 200 with no overview -- either
        # Google produced none, or the deferred 5-credit follow-up failed. Reported apart
        # from real errors: the row cost credits and did not fail.
        out = {"no_ai_overview": 0, "deferred": 0}
        for row in state.get("rows", []):
            ctx = ((row or {}).get("result") or {}).get("context") or {}
            sd = ctx.get("scrapedo") if isinstance(ctx.get("scrapedo"), dict) else {}
            if sd.get("billed_no_overview"):
                out["no_ai_overview"] += 1
            if sd.get("deferred"):
                out["deferred"] += 1
        return out
    if pipeline != "gsearch":
        return None
    out = {"all_phases": 0, "some_phases": 0}
    for row in state.get("rows", []):
        ctx = ((row or {}).get("result") or {}).get("context") or {}
        phases = ctx.get("formatted_results")
        if not isinstance(phases, list) or not phases:
            continue
        flags = [_phase_is_empty(p) for p in phases]
        considered = [f for f in flags if f is not None]
        if not considered:
            continue
        n_empty = sum(1 for f in considered if f)
        if n_empty == len(considered):
            out["all_phases"] += 1
        elif n_empty:
            out["some_phases"] += 1
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
    scrapedo_no_results = 0
    scrapedo_recovered = 0
    scrapedo_errors = 0
    scrapedo_billed_errors = 0
    scrapedo_search_requests = 0
    scrapedo_search_ok = 0
    scrapedo_aio_requests = 0
    scrapedo_aio_ok = 0
    scrapedo_aio_deferred = 0

    search_seconds = 0.0
    llm_seconds = 0.0
    timed_rows = 0
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
            scrapedo_no_results += int(cb.get("scrapedo_no_results") or 0)
            scrapedo_recovered += int(cb.get("scrapedo_recovered_requests") or 0)
            scrapedo_errors += int(cb.get("scrapedo_error_requests") or 0)
            scrapedo_billed_errors += int(cb.get("scrapedo_billed_errors") or 0)
            scrapedo_search_requests += int(cb.get("scrapedo_search_requests") or 0)
            scrapedo_search_ok += int(cb.get("scrapedo_search_successful") or 0)
            scrapedo_aio_requests += int(cb.get("scrapedo_ai_overview_requests") or 0)
            scrapedo_aio_ok += int(cb.get("scrapedo_ai_overview_successful") or 0)
            scrapedo_aio_deferred += int(cb.get("scrapedo_ai_overview_deferred") or 0)
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
        timing = ctx.get("timing")
        if isinstance(timing, dict) and "search_seconds" in timing:
            search_seconds += float(timing.get("search_seconds") or 0.0)
            llm_seconds += float(timing.get("llm_seconds") or 0.0)
            timed_rows += 1
        llm_usd += float(result.get("gemini_cost_usd") or 0.0)
        for key in ("final_url_selection_ai", "gemini_batch_ai", "mapping_ai"):
            obj = ctx.get(key)
            if isinstance(obj, dict):
                if model is None and obj.get("model"):
                    model = str(obj.get("model"))
                usage = obj.get("usage")
                if isinstance(usage, dict):
                    prompt_tokens += int(usage.get("promptTokenCount", 0) or 0)
                    completion_tokens += int(usage.get("candidatesTokenCount", 0) or 0)
    # is_batch: the gemini_batch block is only seeded when batch post-processing is
    # enabled for this upload (LLM_BATCH), so its presence is the reliable signal.
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
        # "llm" when a confidence model actually ran (gsearch always), else "heuristic".
        # Lets the UI show the confidence chip without guessing from model presence.
        # None for firmographics: it is GIVEN the website, so there is nothing to be
        # confident about, and the UI skips the chip on a null. Its Gemini call is an
        # extraction step, not a confidence step -- `model` is still set, so the cost
        # card keeps its LLM line.
        "confidence_mode": (
            None if str(state.get("pipeline") or "") == "firmographics"
            else ("llm" if model else "heuristic")),
        "is_batch": is_batch,
        "token_usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens},
        "cost": _build_cost(llm_usd, serpwow_searches, billable_searches,
                            scrapedo_requests, scrapedo_credits,
                            scrapedo_ok, scrapedo_failed, scrapedo_billed_empty,
                            scrapedo_no_results, scrapedo_recovered, scrapedo_errors,
                            scrapedo_billed_errors=scrapedo_billed_errors,
                            scrapedo_search_requests=scrapedo_search_requests,
                            scrapedo_search_successful=scrapedo_search_ok,
                            scrapedo_ai_overview_requests=scrapedo_aio_requests,
                            scrapedo_ai_overview_successful=scrapedo_aio_ok,
                            scrapedo_ai_overview_deferred=scrapedo_aio_deferred),
        "processing_seconds_total": state.get("processing_seconds_total"),
    }
    # Outcome/error breakdown is original-row-level; each state row represents one
    # input row.
    outcome_breakdown = {"found": 0, "not_found": 0, "errored": 0}
    by_source: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for row in state.get("rows", []):
        oc = _derive_outcome(row or {}, str(state.get("pipeline") or ""))
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

    # Per-row AVERAGES, not sums: this pipeline interleaves provider and LLM work per row
    # under many concurrent workers, so a sum would read as many times the run's wall clock
    # and mean nothing. Emitted only when rows actually carried the split, so no other
    # pipeline's summary grows a key.
    if timed_rows:
        summary["phase_seconds_avg"] = {
            "provider": round(search_seconds / timed_rows, 2),
            "llm": round(llm_seconds / timed_rows, 2),
        }
    # How the LLM ran, so the UI can say so instead of leaving the user to guess. Batch is
    # gsearch-only machinery; firmographics calls Gemini inline, once per row.
    if model:
        summary["llm_mode"] = "batch" if is_batch else "inline"

    ebd = empty_response_breakdown(state)
    if ebd is not None:
        summary["empty_response_breakdown"] = ebd
    return summary


def _csv_row(r: EntityResult) -> dict[str, Any]:
    return {"company_name": r.company_name,
            "company_local_name": r.company_local_name or "",
            "country": r.country, **result_cells(r)}


def write_outputs(upload_dir: Path, state: dict[str, Any]) -> dict[str, Path]:
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
