#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

# Allow importing project modules when executing from scripts/ directly.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _row_has_gemini_batch_output(row: dict[str, Any]) -> bool:
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    context = result.get("context") if isinstance(result.get("context"), dict) else {}
    gemini_batch_ai = context.get("gemini_batch_ai") if isinstance(context.get("gemini_batch_ai"), dict) else {}
    return bool(gemini_batch_ai.get("used"))


def _count_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    terminal = 0
    unprocessed = 0
    queued = 0
    processing = 0
    pending_batch = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip()
        if status in {"completed", "failed"}:
            terminal += 1
            if not _row_has_gemini_batch_output(row):
                pending_batch += 1
        else:
            unprocessed += 1
            if status == "queued":
                queued += 1
            elif status == "processing":
                processing += 1
    return {
        "terminal": terminal,
        "unprocessed": unprocessed,
        "queued": queued,
        "processing": processing,
        "pending_batch": pending_batch,
    }


async def _maybe_init_rabbitmq(app: Any) -> bool:
    if app.rabbitmq_exchange is not None and app.rabbitmq_queue is not None:
        return False
    await app.init_rabbitmq()
    return True


def _build_partial_batch_requests(app: Any, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    requests_payload: list[dict[str, Any]] = []
    row_refs: list[dict[str, Any]] = []
    jsonl_lines: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_index = int(row.get("row_index", 0) or 0)
        key = f"row-{row_index}"
        prompt = app._build_batch_prompt_for_row(row)
        request_obj = {
            "request": {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "responseMimeType": "application/json",
                },
            },
            "metadata": {"key": key},
        }
        requests_payload.append(request_obj)
        row_refs.append({"row_index": row_index, "key": key})
        jsonl_lines.append(
            json.dumps(
                {
                    "key": key,
                    "request": request_obj["request"],
                },
                ensure_ascii=True,
            )
        )
    return requests_payload, row_refs, "\n".join(jsonl_lines) + ("\n" if jsonl_lines else "")


async def _run_partial_gemini_batch(app: Any, upload_id: str, rows_to_batch: list[dict[str, Any]]) -> dict[str, Any]:
    requests_payload, row_refs, jsonl_text = _build_partial_batch_requests(app, rows_to_batch)
    if not requests_payload:
        return {"submitted_rows": 0, "rows_touched": 0, "rows_completed": 0, "rows_failed": 0}

    batch_model = os.getenv("GEMINI_BATCH_MODEL", os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"))
    await app.write_upload_text_artifact(
        upload_id,
        "batch_input_jsonl",
        jsonl_text,
        "application/x-ndjson; charset=utf-8",
    )
    create_resp = await asyncio.to_thread(
        app._gemini_batch_create_sync,
        batch_model,
        requests_payload,
        upload_id,
    )
    batch_name = str(create_resp.get("name") or "")
    if not batch_name:
        raise RuntimeError(f"Unexpected Gemini batch create response: {create_resp}")
    print(
        f"[helper] submitted partial Gemini batch upload_id={upload_id} "
        f"job_name={batch_name} rows={len(row_refs)} model={batch_model}"
    )

    poll_interval = max(5, int(os.getenv("GEMINI_BATCH_POLL_SEC", "15") or 15))
    poll_timeout = max(60, int(os.getenv("GEMINI_BATCH_TIMEOUT_SEC", "1800") or 1800))
    deadline = time.monotonic() + poll_timeout
    while True:
        batch_obj = await asyncio.to_thread(app._gemini_batch_get_sync, batch_name)
        state_name = app._gemini_batch_state_name(batch_obj)
        done_flag = bool(batch_obj.get("done"))
        if app._gemini_batch_is_terminal(state_name, done_flag):
            final_batch_obj = batch_obj
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Gemini batch timed out after {poll_timeout}s for job={batch_name}")
        await asyncio.sleep(poll_interval)

    await app.write_upload_text_artifact(
        upload_id,
        "batch_output_json",
        json.dumps(final_batch_obj, ensure_ascii=True, indent=2),
        "application/json; charset=utf-8",
    )

    final_state_name = app._gemini_batch_state_name(final_batch_obj if isinstance(final_batch_obj, dict) else {})
    done_flag = bool(final_batch_obj.get("done")) if isinstance(final_batch_obj, dict) else False
    if not app._gemini_batch_is_success(
        final_state_name, done_flag, final_batch_obj if isinstance(final_batch_obj, dict) else {}
    ):
        error_obj = final_batch_obj.get("error") if isinstance(final_batch_obj, dict) else None
        raise RuntimeError(f"Gemini batch ended with state={final_state_name} error={error_obj}")

    inlined = app._extract_batch_inlined_responses(final_batch_obj if isinstance(final_batch_obj, dict) else {})
    row_index_by_key: dict[str, int] = {
        str(ref.get("key") or "").strip(): int(ref.get("row_index", 0) or 0)
        for ref in row_refs
        if isinstance(ref, dict) and str(ref.get("key") or "").strip()
    }
    use_index_fallback = len(inlined) == len(row_refs)
    parsed_by_row: dict[int, dict[str, Any]] = {}
    usage_by_row: dict[int, dict[str, Any]] = {}
    usage_total_prompt = 0
    usage_total_candidates = 0
    for idx, inline_item in enumerate(inlined):
        if not isinstance(inline_item, dict):
            continue
        response_key = app._extract_batch_response_key(inline_item)
        row_index = row_index_by_key.get(response_key) if response_key else None
        if row_index is None and use_index_fallback and idx < len(row_refs):
            row_index = row_refs[idx]["row_index"]
        if row_index is None:
            continue
        response_obj = inline_item.get("response") if isinstance(inline_item.get("response"), dict) else {}
        text = app._extract_text_from_generate_response(response_obj)
        parsed = app._parse_json_from_text(text) or {}
        usage = response_obj.get("usageMetadata") if isinstance(response_obj.get("usageMetadata"), dict) else {}
        usage_total_prompt += int(usage.get("promptTokenCount", 0) or 0)
        usage_total_candidates += int(usage.get("candidatesTokenCount", 0) or 0)
        parsed_by_row[int(row_index)] = parsed
        usage_by_row[int(row_index)] = usage

    target_rows = {int(ref["row_index"]) for ref in row_refs if isinstance(ref, dict) and ref.get("row_index") is not None}
    batch_usage = {"promptTokenCount": usage_total_prompt, "candidatesTokenCount": usage_total_candidates}

    rows_touched = 0
    rows_completed = 0
    rows_failed = 0
    async with app.get_upload_lock(upload_id):
        state = await app.read_upload_artifact(upload_id, "state")
        for row in state.get("rows", []):
            if not isinstance(row, dict):
                continue
            row_index = int(row.get("row_index", 0) or 0)
            if row_index not in target_rows:
                continue
            parsed = parsed_by_row.get(row_index)
            if not isinstance(parsed, dict):
                continue
            rows_touched += 1

            result = row.get("result") if isinstance(row.get("result"), dict) else {}
            context = result.get("context") if isinstance(result.get("context"), dict) else {}
            row_usage = usage_by_row.get(row_index) if isinstance(usage_by_row.get(row_index), dict) else {}
            row_batch_cost_usd = app.calculate_gemini_batch_cost_usd(row_usage)
            prev_gemini_cost = app._as_float(result.get("gemini_cost_usd"), 0.0)
            prev_total_cost = app._as_float(result.get("total_cost_usd"), 0.0)
            updated_gemini_cost = round(prev_gemini_cost + row_batch_cost_usd, 8)
            updated_total_cost = round(prev_total_cost + row_batch_cost_usd, 8)

            selected_url = parsed.get("official_website")
            if isinstance(selected_url, str) and selected_url.strip() and not app.is_disallowed_official_url(selected_url):
                candidate_url = selected_url.strip()
                if app._official_website_looks_plausible(
                    candidate_url,
                    str(row.get("company_name") or ""),
                    str(row.get("country") or ""),
                ):
                    result["official_website"] = candidate_url

            result["summary"] = str(parsed.get("summary") or result.get("summary") or "")
            if parsed.get("website_company_descirption_ai") is not None:
                result["website_company_descirption_ai"] = parsed.get("website_company_descirption_ai")
            if parsed.get("website_company_descirption_translated_ai") is not None:
                result["website_company_descirption_translated_ai"] = parsed.get(
                    "website_company_descirption_translated_ai"
                )
            if parsed.get("address") is not None:
                result["address"] = parsed.get("address")
            if parsed.get("phone") is not None:
                result["phone"] = parsed.get("phone")
            if parsed.get("email") is not None:
                result["email"] = parsed.get("email")
            if parsed.get("industry") is not None:
                result["industry"] = parsed.get("industry")
            if isinstance(parsed.get("products"), list):
                result["products"] = parsed.get("products")
            if isinstance(parsed.get("services"), list):
                result["services"] = parsed.get("services")
            result["gemini_cost_usd"] = updated_gemini_cost
            result["total_cost_usd"] = updated_total_cost

            cost_breakdown = context.get("cost_breakdown") if isinstance(context.get("cost_breakdown"), dict) else {}
            cost_breakdown["gemini_batch_cost_usd"] = round(
                app._as_float(cost_breakdown.get("gemini_batch_cost_usd"), 0.0) + row_batch_cost_usd,
                8,
            )
            cost_breakdown["gemini_cost_usd"] = updated_gemini_cost
            cost_breakdown["total_cost_usd"] = updated_total_cost
            context["cost_breakdown"] = cost_breakdown
            context["gemini_batch_ai"] = {
                "provider": "google-gemini-batch",
                "model": batch_model,
                "used": True,
                "usage": row_usage,
                "cost_usd": row_batch_cost_usd,
                "raw": parsed,
                "error": None,
            }
            result["context"] = context
            row["result"] = result

            finalized_official = str(result.get("official_website") or "").strip()
            is_plausible_official = (
                bool(finalized_official)
                and app._official_website_looks_plausible(
                    finalized_official,
                    str(row.get("company_name") or ""),
                    str(row.get("country") or ""),
                )
            )
            if not is_plausible_official:
                result["official_website"] = None
            row["status"] = "completed" if is_plausible_official else "failed"
            row["error"] = None if row["status"] == "completed" else (
                "Official website not found after Gemini batch post-processing."
            )
            if row["status"] == "completed":
                rows_completed += 1
            else:
                rows_failed += 1

        # Keep top-level metadata lightweight; this helper intentionally runs incremental batches.
        state["gemini_batch_recovery"] = {
            "status": "succeeded",
            "completed_at": app._now_iso(),
            "submitted_rows": len(row_refs),
            "rows_with_output": len(parsed_by_row),
            "rows_touched": rows_touched,
            "rows_completed": rows_completed,
            "rows_failed": rows_failed,
            "usage": batch_usage,
            "batch_cost_usd": app.calculate_gemini_batch_cost_usd(batch_usage),
        }
        await app.persist_upload_state(upload_id, state)

    return {
        "submitted_rows": len(row_refs),
        "rows_with_output": len(parsed_by_row),
        "rows_touched": rows_touched,
        "rows_completed": rows_completed,
        "rows_failed": rows_failed,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detect unprocessed rows for an upload, requeue queued work, wait for processing, "
            "then submit only rows not yet Gemini-batch-postprocessed."
        )
    )
    parser.add_argument("upload_id", help="Upload ID")
    parser.add_argument(
        "--wait-timeout-sec",
        type=int,
        default=1800,
        help="Max seconds to wait for unprocessed rows to become terminal (default: 1800)",
    )
    parser.add_argument(
        "--poll-sec",
        type=int,
        default=10,
        help="Polling interval while waiting (default: 10)",
    )
    parser.add_argument(
        "--skip-wait",
        action="store_true",
        help="Do not wait; immediately submit currently pending processed rows.",
    )
    args = parser.parse_args()

    try:
        from app.services.serpwow import legacy_app as app
    except ModuleNotFoundError as exc:
        print(
            f"Missing dependency while importing app: {exc}. "
            "Run with the same Python env as the server.",
            file=sys.stderr,
        )
        return 1

    upload_id = str(args.upload_id).strip()
    if not upload_id:
        print("upload_id is required", file=sys.stderr)
        return 2

    rabbit_initialized_here = False
    try:
        state = await app.get_upload_state(upload_id)
        pipeline = str(state.get("pipeline") or app.PIPELINE_FULL)
        if pipeline != app.PIPELINE_FULL:
            print(
                f"Upload pipeline is '{pipeline}', expected '{app.PIPELINE_FULL}' for Gemini batch post-processing.",
                file=sys.stderr,
            )
            return 1

        rows = state.get("rows") if isinstance(state.get("rows"), list) else []
        counts = _count_rows(rows)
        print(json.dumps({"upload_id": upload_id, "initial": counts}, ensure_ascii=True, indent=2))

        if counts["unprocessed"] > 0:
            rabbit_initialized_here = await _maybe_init_rabbitmq(app)

        # First recovery tick.
        if counts["unprocessed"] > 0:
            state = await app.maybe_requeue_stuck_queued_rows(upload_id, state)
            state = await app.maybe_fail_stale_processing_rows(upload_id, state)

        if not args.skip_wait:
            deadline = time.monotonic() + max(1, int(args.wait_timeout_sec))
            while True:
                state = await app.get_upload_state(upload_id)
                rows = state.get("rows") if isinstance(state.get("rows"), list) else []
                counts = _count_rows(rows)
                print(
                    f"[wait] unprocessed={counts['unprocessed']} queued={counts['queued']} "
                    f"processing={counts['processing']} pending_batch={counts['pending_batch']}"
                )
                if counts["unprocessed"] == 0:
                    break
                if time.monotonic() >= deadline:
                    print(
                        f"[wait] timeout reached after {int(args.wait_timeout_sec)}s; continuing with currently eligible rows.",
                        file=sys.stderr,
                    )
                    break
                # Opportunistic rescue each tick.
                state = await app.maybe_requeue_stuck_queued_rows(upload_id, state)
                state = await app.maybe_fail_stale_processing_rows(upload_id, state)
                await asyncio.sleep(max(1, int(args.poll_sec)))

        # Re-read and select only rows that are terminal but not yet Gemini-batch-postprocessed.
        state = await app.get_upload_state(upload_id)
        rows = state.get("rows") if isinstance(state.get("rows"), list) else []
        rows_to_batch = [
            row for row in rows
            if isinstance(row, dict)
            and row.get("status") in {"completed", "failed"}
            and not _row_has_gemini_batch_output(row)
        ]

        pre_submit_counts = _count_rows(rows)
        print(json.dumps({"upload_id": upload_id, "before_submit": pre_submit_counts}, ensure_ascii=True, indent=2))

        if not rows_to_batch:
            print("No remaining processed rows pending Gemini batch post-processing.")
            return 0

        outcome = await _run_partial_gemini_batch(app, upload_id, rows_to_batch)
        print("Partial Gemini batch finished:")
        print(json.dumps(outcome, ensure_ascii=True, indent=2))
        return 0
    finally:
        if rabbit_initialized_here:
            try:
                await app.close_rabbitmq()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
