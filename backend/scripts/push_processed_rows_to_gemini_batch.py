#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import sys

# Allow importing project modules when executing from scripts/ directly.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Push already-processed rows of an upload to Gemini batch."
    )
    parser.add_argument("upload_id", help="Upload ID to batch-process")
    parser.add_argument(
        "--force-new-job",
        action="store_true",
        help="Reset local batch metadata and submit a new Gemini batch job",
    )
    args = parser.parse_args()
    try:
        from app.services.serpwow import engine as app
    except ModuleNotFoundError as exc:
        print(
            f"Missing dependency while importing app: {exc}. "
            "Run this script with the same Python environment used by the server (or install requirements).",
            file=sys.stderr,
        )
        return 1

    upload_id = str(args.upload_id).strip()
    if not upload_id:
        print("upload_id is required", file=sys.stderr)
        return 2

    try:
        state = await app.get_upload_state(upload_id)
    except KeyError:
        print(f"Upload not found: {upload_id}", file=sys.stderr)
        return 1

    pipeline = str(state.get("pipeline") or app.PIPELINE_FULL)
    if pipeline != app.PIPELINE_FULL:
        print(
            f"Upload pipeline is '{pipeline}', expected '{app.PIPELINE_FULL}' for Gemini batch post-processing.",
            file=sys.stderr,
        )
        return 1

    rows = state.get("rows") if isinstance(state.get("rows"), list) else []
    eligible = [
        row for row in rows
        if isinstance(row, dict) and row.get("status") in {"completed", "failed"}
    ]

    print(
        json.dumps(
            {
                "upload_id": upload_id,
                "pipeline": pipeline,
                "total_rows": len(rows),
                "eligible_rows_for_batch": len(eligible),
                "current_batch": state.get("gemini_batch"),
            },
            ensure_ascii=True,
            indent=2,
        )
    )

    if not eligible:
        print("No completed/failed rows available to send to Gemini batch.")
        return 0

    if args.force_new_job:
        async with app.get_upload_lock(upload_id):
            latest = await app.read_upload_artifact(upload_id, "state")
            latest["gemini_batch"] = {
                "status": "waiting_for_rows",
                "queued_at": None,
                "job_name": None,
                "error": None,
            }
            await app.persist_upload_state(upload_id, latest)
        print("Local gemini_batch metadata reset (force-new-job).")

    print("Starting Gemini batch processing for processed rows...")
    await app.run_gemini_batch_for_upload(upload_id)

    final_state = await app.get_upload_state(upload_id)
    final_batch = final_state.get("gemini_batch") if isinstance(final_state.get("gemini_batch"), dict) else {}
    print("Final gemini_batch state:")
    print(json.dumps(final_batch, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
