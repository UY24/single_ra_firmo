"""End-to-end integration test for the gsearch batch pipeline.

Flow exercised (no real cloud services):
  1. Build upload state with 6 rows directly (bypassing HTTP + RabbitMQ).
  2. Call process_upload_job() for 5 rows (rows 1-5); leave row 6 stuck ("queued").
  3. Call reconcile_stuck_gsearch_rows() → row 6 force-failed via reconciler.
  4. After all rows are terminal, maybe_start_gemini_batch_for_upload triggers
     run_gemini_batch_for_upload with GEMINI_BATCH_SHARD_SIZE=2 → ≥2 chunks.
     Chunk 1 (the second chunk) is patched to JOB_STATE_FAILED.
  5. persist_upload_state fires _finalize_serpwow_outputs → found.csv / notFound.csv.
  6. Slack notify_run_complete is patched to capture calls.
  7. _update_supabase_run is patched to capture calls.
  8. Assertions verify all acceptance criteria.

Acceptance assertions:
  A1 - every row terminal; row 6 = failed (by reconciler).
  A2 - gemini_batch has ≥2 chunks, exactly one failed → status = completed_with_errors.
  A3 - notify_run_complete called EXACTLY ONCE.
  A4 - found.csv count == supabase websites_found == slack success;
       notFound.csv count == supabase websites_not_found == slack failed.
  A5 - GET /uploads/{id}/result?file=found.csv serves found.csv content.
"""
import asyncio
import csv
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

from app.services.serpwow import engine as app
from app.core import notify as notify_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale_iso(age_sec: int = 9999) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=age_sec)).isoformat()


def _make_row(idx: int, status: str = "queued", stale: bool = False) -> dict:
    ts = _stale_iso() if stale else _now_iso()
    return {
        "row_index": idx,
        "company_name": f"Corp {idx}",
        "country": "US",
        "firm_id": "",
        "industry": "",
        "full_address": "",
        "official_website": "",
        "status": status,
        "status_updated_at": ts,
        "processing_started_at": None,
        "requeue_attempts": 0,
        "error": None,
        "result": None,
    }


def _make_state(upload_id: str, rows: list) -> dict:
    """Build a state dict that mirrors _create_upload_with_rows in batch mode.

    Key difference from a raw dict: includes ``gemini_batch`` pre-seeded with
    ``status="waiting_for_rows"`` so ``_batch_postprocess_pending`` returns True
    from the first persist call, deferring Slack/Supabase until the batch is done.
    """
    return {
        "upload_id": upload_id,
        "company_id": "company-test",
        "company_name": "Test Co",
        "run_db_id": "run-db-id-001",
        "pipeline": "gsearch",
        "phase": "all",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "status": "queued",
        "total_rows": len(rows),
        "processed_rows": 0,
        "success_rows": 0,
        "failed_rows": 0,
        "rows": rows,
        # Pre-seed the batch block so _batch_postprocess_pending returns True
        # from the first persist call, deferring Slack until the batch is done.
        # This matches _create_upload_with_rows when LLM_BATCH=true.
        "gemini_batch": {
            "status": "waiting_for_rows",
            "queued_at": None,
            "job_name": None,
            "error": None,
        },
    }


def _fake_gsearch_response(company_name: str, country: str, has_website: bool = True):
    """Return a CrawlResponse-like coroutine for a gsearch worker call."""
    from app.services.serpwow.engine import CrawlResponse
    url = f"https://example-{company_name.lower().replace(' ', '-')}.com" if has_website else None
    result = CrawlResponse(
        company_name=company_name,
        country=country,
        official_website=url,
        summary="",
        massive_proxy_cost_usd=0.0,
        serpwow_cost_usd=0.0,
        gemini_cost_usd=0.0,
        total_cost_usd=0.0,
        context={
            "candidates": [url] if url else [],
            "cost_breakdown": {"serpwow_request_count": 1},
            "final_url_selection_ai": {"used": False, "raw": {}},
        },
    )
    return result, "{}"


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

class TestGsearchE2E(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        """Set up temp upload dir and patch all external services."""
        self.tmp = tempfile.mkdtemp(prefix="gsearch_e2e_")
        self.upload_id = "e2e-test-upload-001"

        # Patch UPLOAD_BASE_DIR so all disk I/O goes to our temp dir.
        patcher = mock.patch.object(app, "UPLOAD_BASE_DIR", Path(self.tmp))
        self.addCleanup(patcher.stop)
        patcher.start()

        # Create upload dir.
        upload_dir = Path(self.tmp) / self.upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)

        # Environment: batch mode ON, small chunk size for ≥2 chunks, no real cloud.
        self.env = {
            "LLM_BATCH": "true",
            "GEMINI_BATCH_SHARD_SIZE": "2",   # 6 rows → 3 chunks
            "GEMINI_BATCH_MAX_INFLIGHT": "10",
            "ENABLE_FINAL_URL_GEMINI": "false",  # skip per-row LLM
            "GSEARCH_ROW_STALE_TIMEOUT_SEC": "60",
            "GSEARCH_ROW_MAX_REQUEUE": "0",      # force-fail immediately
            "GEMINI_API_KEY": "fake-key",
            "S3_BUCKET": "",
            "SUPABASE_URL": "",
            "SUPABASE_SERVICE_ROLE_KEY": "",
            "SLACK_WEBHOOK_URL": "",
        }
        env_patcher = mock.patch.dict("os.environ", self.env, clear=False)
        self.addCleanup(env_patcher.stop)
        env_patcher.start()

        # Capture calls to Supabase and Slack.
        self.supabase_calls: list[dict] = []
        self.slack_calls: list[dict] = []

        def fake_update_supabase(state: dict) -> bool:
            self.supabase_calls.append(dict(state))
            return True

        def fake_notify_complete(**kwargs):
            self.slack_calls.append(dict(kwargs))
            return True

        supabase_patcher = mock.patch.object(app, "_update_supabase_run", side_effect=fake_update_supabase)
        self.addCleanup(supabase_patcher.stop)
        supabase_patcher.start()

        notify_patcher = mock.patch.object(notify_module, "notify_run_complete", side_effect=fake_notify_complete)
        self.addCleanup(notify_patcher.stop)
        notify_patcher.start()

        # Patch _mark_supabase_run_running so it never tries to reach Supabase.
        mark_running_patcher = mock.patch.object(app, "_mark_supabase_run_running", return_value=True)
        self.addCleanup(mark_running_patcher.stop)
        mark_running_patcher.start()

        # Patch upload_raw_response_to_s3 to be a no-op (no real S3).
        s3_patcher = mock.patch.object(
            app, "upload_raw_response_to_s3",
            new=mock.AsyncMock(return_value=(None, None))
        )
        self.addCleanup(s3_patcher.stop)
        s3_patcher.start()

        # Patch execute_gsearch_lookup_for_worker: rows 1-4 found, row 5 not found.
        # Row 6 stays stuck (we never process it), then reconciler force-fails it.
        async def fake_gsearch(company_name, country, **kwargs):
            # Rows for companies: "Corp 1"..."Corp 5"; "Corp 4" and "Corp 5" → no website.
            has_url = company_name not in ("Corp 4", "Corp 5")
            return _fake_gsearch_response(company_name, country, has_website=has_url)

        gsearch_patcher = mock.patch.object(app, "execute_gsearch_lookup_for_worker",
                                            side_effect=fake_gsearch)
        self.addCleanup(gsearch_patcher.stop)
        gsearch_patcher.start()

        # Patch gemini_batch chunk functions.
        # 6 rows with chunk_size=2 → 3 chunks (chunk 0, 1, 2).
        # Chunk 2 (the LAST chunk, containing rows 5 and 6) will be JOB_STATE_FAILED.
        # This ensures row 6 (reconciler-force-failed) remains without a URL in the
        # final state, and rows 5+6 end up in notFound.csv.
        self._chunk_counter = 0

        def fake_create(model, items, display_name):
            n = self._chunk_counter
            self._chunk_counter += 1
            keys = [k for k, _ in items]
            # Fail chunk 2 (the third/last chunk, rows 5-6).
            job_name = "jobs/FAIL_CHUNK2" if n == 2 else f"jobs/OK_CHUNK{n}"
            return {"name": job_name, "_keys": keys}

        def fake_get(name):
            state_val = "JOB_STATE_FAILED" if "FAIL" in name else "JOB_STATE_SUCCEEDED"
            return {"name": name, "done": True, "state": {"name": state_val}}

        def fake_collect(obj):
            if "FAIL" in (obj.get("name") or ""):
                return []
            out = []
            for k in obj.get("_keys", []):
                # key format is "row-<idx>"
                idx = int(k.split("-")[1])
                out.append({
                    "key": k,
                    "text": json.dumps({"official_website": f"https://example-corp-{idx}.com",
                                        "confidence_score": 85, "confidence": "high",
                                        "reason": "direct match", "alternatives": []}),
                    "usage": {"promptTokenCount": 100, "candidatesTokenCount": 20},
                })
            return out

        gb_create_patcher = mock.patch("app.services.ai_mode.gemini_batch.create_batch",
                                       side_effect=fake_create)
        gb_get_patcher = mock.patch("app.services.ai_mode.gemini_batch.get_batch",
                                    side_effect=fake_get)
        gb_collect_patcher = mock.patch("app.services.ai_mode.gemini_batch.collect_results",
                                        side_effect=fake_collect)
        self.addCleanup(gb_create_patcher.stop)
        self.addCleanup(gb_get_patcher.stop)
        self.addCleanup(gb_collect_patcher.stop)
        gb_create_patcher.start()
        gb_get_patcher.start()
        gb_collect_patcher.start()

        # Patch write_upload_text_artifact to be a no-op (no S3).
        wta_patcher = mock.patch.object(app, "write_upload_text_artifact", new=mock.AsyncMock())
        self.addCleanup(wta_patcher.stop)
        wta_patcher.start()

        # Patch rabbitmq_exchange and rabbitmq_queue so reconciler boots but
        # publish_job is captured (won't actually send to broker).
        rmq_exc = mock.MagicMock()
        rmq_q = mock.MagicMock()
        exc_patcher = mock.patch.object(app, "rabbitmq_exchange", rmq_exc)
        q_patcher = mock.patch.object(app, "rabbitmq_queue", rmq_q)
        self.addCleanup(exc_patcher.stop)
        self.addCleanup(q_patcher.stop)
        exc_patcher.start()
        q_patcher.start()

        # Patch _get_rabbitmq_queue_depth to always return 0 (drained) for reconciler.
        depth_patcher = mock.patch.object(app, "_get_rabbitmq_queue_depth",
                                          new=mock.AsyncMock(return_value=0))
        self.addCleanup(depth_patcher.stop)
        depth_patcher.start()

        # Patch publish_job so reconciler's requeue attempt is a no-op (we set max=0
        # so it goes straight to force-fail anyway).
        pj_patcher = mock.patch.object(app, "publish_job", new=mock.AsyncMock())
        self.addCleanup(pj_patcher.stop)
        pj_patcher.start()

        # Flush the gemini_batch_tasks dict so a prior task can't bleed in.
        app.gemini_batch_tasks.clear()

    async def _build_and_write_initial_state(self) -> dict:
        """Create 6-row queued state and write it to disk."""
        rows = [_make_row(i) for i in range(1, 7)]
        state = _make_state(self.upload_id, rows)
        state_path = Path(self.tmp) / self.upload_id / "state.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return state

    async def _drain_gemini_batch_task(self):
        """Wait for the background gemini_batch_tasks task to complete."""
        task = app.gemini_batch_tasks.get(self.upload_id)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                pass

    async def test_full_e2e_pipeline(self):
        """Drive the full gsearch batch pipeline end-to-end."""
        # ------------------------------------------------------------------ #
        # Phase 1: Build initial state with 6 queued rows.
        # ------------------------------------------------------------------ #
        await self._build_and_write_initial_state()

        # ------------------------------------------------------------------ #
        # Phase 2: Process rows 1-5, leave row 6 stuck in "queued".
        # ------------------------------------------------------------------ #
        for idx in range(1, 6):
            job = {
                "upload_id": self.upload_id,
                "row_index": idx,
                "company_name": f"Corp {idx}",
                "country": "US",
                "pipeline": "gsearch",
                "phase": "all",
                "upload_company_name": "Test Co",
            }
            # process_upload_job may kick off a gemini batch task at the end
            # of each row (when all rows are terminal). We want it to fire only
            # after all rows are done, so we cancel any premature task.
            await app.process_upload_job(job)
            # Cancel any task started before row 6 is decided (it would see
            # only 5/6 rows terminal and the state is still "processing").
            task = app.gemini_batch_tasks.get(self.upload_id)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                app.gemini_batch_tasks.pop(self.upload_id, None)

        # ------------------------------------------------------------------ #
        # Phase 3: Reconciler force-fails row 6.
        # ------------------------------------------------------------------ #
        # Make row 6 appear stale so reconciler acts on it.
        state_path = Path(self.tmp) / self.upload_id / "state.json"
        state = json.loads(state_path.read_text())
        for row in state["rows"]:
            if row["row_index"] == 6:
                row["status_updated_at"] = _stale_iso(9999)
                row["processing_started_at"] = None
                row["requeue_attempts"] = 0
        state_path.write_text(json.dumps(state), encoding="utf-8")

        # Patch _collect_states_for_reconcile + _list_local_state_files_sync to
        # return only our upload.
        fresh_state = json.loads(state_path.read_text())
        with mock.patch.object(app, "_collect_states_for_reconcile",
                               new=mock.AsyncMock(return_value=[fresh_state])), \
             mock.patch.object(app, "_list_local_state_files_sync", return_value=[]):
            await app.reconcile_stuck_gsearch_rows()

        # Capture state right after reconciler to verify row 6 was force-failed.
        post_reconcile_state = json.loads(state_path.read_text())
        self._post_reconcile_row6 = next(
            r for r in post_reconcile_state["rows"] if r["row_index"] == 6
        )

        # ------------------------------------------------------------------ #
        # Phase 4: Wait for the gemini batch task that reconciler triggered.
        # ------------------------------------------------------------------ #
        # After reconciler calls persist_upload_state (all 6 rows terminal),
        # maybe_start_gemini_batch_for_upload should launch a task.
        await self._drain_gemini_batch_task()

        # ------------------------------------------------------------------ #
        # Read final state.
        # ------------------------------------------------------------------ #
        final_state_path = Path(self.tmp) / self.upload_id / "state.json"
        final_state = json.loads(final_state_path.read_text())

        # ================================================================== #
        # A1: Every row is terminal; row 6 was force-failed by the reconciler.
        # ================================================================== #
        rows = final_state.get("rows", [])
        for row in rows:
            self.assertIn(
                row["status"], {"completed", "failed"},
                f"Row {row['row_index']} is not terminal: {row['status']!r}"
            )

        # Verify row 6 was force-failed by the reconciler (captured right after reconciler ran).
        row6_post_reconcile = self._post_reconcile_row6
        self.assertEqual(row6_post_reconcile["status"], "failed",
                         "Row 6 should be force-failed by reconciler (pre-batch check)")
        self.assertIn("terminalized", (row6_post_reconcile.get("error") or "").lower(),
                      "Row 6 error should mention 'terminalized'")

        # After batch: row 6 is in the failing chunk → no URL → remains failed.
        row6_final = next(r for r in rows if r["row_index"] == 6)
        self.assertEqual(row6_final["status"], "failed",
                         "Row 6 should remain failed after batch (in failing chunk 2)")

        # ================================================================== #
        # A2: Gemini batch has ≥2 chunks; one chunk failed → completed_with_errors.
        # ================================================================== #
        gb = final_state.get("gemini_batch", {})
        chunks = gb.get("chunks", [])
        self.assertGreaterEqual(len(chunks), 2, "Expected ≥2 batch chunks")
        chunk_statuses = [c["status"] for c in chunks]
        self.assertIn("failed", chunk_statuses, "At least one chunk should have failed")
        self.assertIn("succeeded", chunk_statuses, "At least one chunk should have succeeded")
        self.assertEqual(gb.get("status"), "completed_with_errors",
                         "Aggregate batch status should be completed_with_errors")

        # ================================================================== #
        # A3: notify_run_complete called EXACTLY ONCE.
        # ================================================================== #
        self.assertEqual(len(self.slack_calls), 1,
                         f"notify_run_complete must be called exactly once; got {len(self.slack_calls)}")

        # ================================================================== #
        # A4: found.csv count == supabase websites_found == slack success;
        #     notFound.csv count == supabase websites_not_found == slack failed.
        # ================================================================== #
        found_csv = Path(self.tmp) / self.upload_id / "found.csv"
        not_found_csv = Path(self.tmp) / self.upload_id / "notFound.csv"
        self.assertTrue(found_csv.exists(), "found.csv must exist")
        self.assertTrue(not_found_csv.exists(), "notFound.csv must exist")

        with found_csv.open(newline="", encoding="utf-8") as fh:
            found_rows = list(csv.DictReader(fh))
        with not_found_csv.open(newline="", encoding="utf-8") as fh:
            not_found_rows = list(csv.DictReader(fh))

        found_count = len(found_rows)
        not_found_count = len(not_found_rows)

        # Verify found.csv rows actually have a URL.
        for row in found_rows:
            self.assertTrue(row.get("website_url", "").strip(),
                            f"found.csv row has no website_url: {row}")

        # Supabase call: should have been called with websites_found / websites_not_found.
        self.assertGreaterEqual(len(self.supabase_calls), 1,
                                "_update_supabase_run must have been called")
        # Take the last call (terminal one).
        last_supabase = self.supabase_calls[-1]
        from app.services.serpwow import reporting
        results = reporting.state_to_entity_results(last_supabase)
        summary = reporting.build_summary(last_supabase, results)
        supabase_found = summary["websites_found"]
        supabase_not_found = summary["websites_not_found"]

        self.assertEqual(found_count, supabase_found,
                         f"found.csv rows ({found_count}) != supabase websites_found ({supabase_found})")
        self.assertEqual(not_found_count, supabase_not_found,
                         f"notFound.csv rows ({not_found_count}) != supabase websites_not_found ({supabase_not_found})")

        # Slack (Task 9): gsearch is a REPORTING_PIPELINES member, so the ping now
        # carries the found/not_found/errored trio off build_summary's
        # outcome_breakdown -- not the old success/failed pair.
        slack_payload = self.slack_calls[0]
        self.assertNotIn("success", slack_payload)
        self.assertNotIn("failed", slack_payload)
        slack_found = slack_payload.get("found")
        slack_not_found = slack_payload.get("not_found")
        slack_errored = slack_payload.get("errored")
        self.assertEqual(slack_found, summary["outcome_breakdown"]["found"],
                         "Slack found count must match outcome_breakdown['found']")
        self.assertEqual(slack_not_found, summary["outcome_breakdown"]["not_found"],
                         "Slack not_found count must match outcome_breakdown['not_found']")
        self.assertEqual(slack_errored, summary["outcome_breakdown"]["errored"],
                         "Slack errored count must match outcome_breakdown['errored']")

        # Cross-check: found_count == slack_success doesn't hold directly because
        # success_rows counts rows with status=completed (which includes "pending batch"
        # completions that the batch then promotes OR demotes). Instead assert the
        # triple: found_count + not_found_count == total rows, and each matches supabase.
        self.assertEqual(found_count + not_found_count, len(rows),
                         "found + notFound rows must equal total rows")

        # ================================================================== #
        # A5: Result endpoint serves found.csv.
        # ================================================================== #
        from fastapi.testclient import TestClient
        client = TestClient(app.app)

        # The endpoint reads from disk using _find_upload_dir which uses UPLOAD_BASE_DIR.
        # Since we've already patched UPLOAD_BASE_DIR, the file is at the right path.
        response = client.get(f"/uploads/{self.upload_id}/result?file=found.csv")
        self.assertEqual(response.status_code, 200,
                         f"Result endpoint returned {response.status_code}: {response.text[:200]}")
        content = response.content.decode("utf-8")
        served_rows = list(csv.DictReader(io.StringIO(content)))
        self.assertEqual(len(served_rows), found_count,
                         f"Served found.csv has {len(served_rows)} rows, expected {found_count}")
        self.assertIn("text/csv", response.headers.get("content-type", ""),
                      "Content-type must be text/csv")


if __name__ == "__main__":
    unittest.main()
