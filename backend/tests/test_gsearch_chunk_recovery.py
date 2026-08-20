"""Recovery tests for the chunked Gemini batch driver (run_gemini_batch_for_upload).

Covers the 4 recovery scenarios from spec §8c:
  1. Resume after restart (chunk 0 already succeeded, chunk 1 running with job_name)
  2. Batch timeout (get_batch never returns terminal; chunk fails, aggregate = failed/cwe)
  3. Partial output (collect_results returns a subset of keys; missing → not-found)
  4. Re-finalize after /retry-failed-rows (reset to waiting_for_rows, re-run driver)

Mock structure mirrors test_gsearch_chunked.py exactly.
"""
import asyncio
import json
import unittest
from unittest import mock

from app.services.serpwow import engine as app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pending_row(idx, candidate="https://c%d.com"):
    """A row in the 'completed' status waiting for Gemini batch post-processing."""
    url = candidate % idx
    return {
        "row_index": idx,
        "company_name": f"C{idx}",
        "country": "US",
        "status": "completed",
        "error": "Pending Gemini batch post-processing decision.",
        "result": {
            "official_website": None,
            "context": {
                "candidates": [url],
                "cost_breakdown": {"serpwow_request_count": 1},
            },
        },
    }


def _already_done_row(idx, url_tpl="https://c%d.com"):
    """A row that already has official_website set (chunk 0 succeeded in prior run)."""
    url = url_tpl % idx
    return {
        "row_index": idx,
        "company_name": f"C{idx}",
        "country": "US",
        "status": "completed",
        "error": None,
        "result": {
            "official_website": url,
            "context": {
                "candidates": [url],
                "cost_breakdown": {"serpwow_request_count": 1},
            },
        },
    }


def _failed_row(idx, candidate="https://c%d.com"):
    """A row that failed (not-found) in a prior run."""
    url = candidate % idx
    return {
        "row_index": idx,
        "company_name": f"C{idx}",
        "country": "US",
        "status": "failed",
        "error": "Official website not found after Gemini batch post-processing.",
        "result": {
            "official_website": None,
            "context": {
                "candidates": [url],
                "cost_breakdown": {"serpwow_request_count": 1},
            },
        },
    }




# ---------------------------------------------------------------------------
# Scenario 1: Resume after restart
# ---------------------------------------------------------------------------

class TestResumeAfterRestart(unittest.IsolatedAsyncioTestCase):
    """Chunk 0 already succeeded (with job_name); chunk 1 is 'running' (has job_name).
    The driver must NOT call create_batch for chunk 0 (it's already done) and must
    re-poll chunk 1's existing job. Final aggregate must be terminal."""

    async def test_resume_skips_succeeded_chunk_and_reppolls_running_chunk(self):
        # 10 rows split into chunk0 (5 rows, idx 1-5) and chunk1 (5 rows, idx 6-10)
        # chunk0 already succeeded: rows already have official_website set
        # chunk1 was running: rows are still in 'completed' pending state
        chunk0_rows = [_already_done_row(i) for i in range(1, 6)]
        chunk1_rows = [_pending_row(i) for i in range(6, 11)]
        rows = chunk0_rows + chunk1_rows

        existing_chunks = [
            {"chunk_id": 0, "job_name": "jobs/CHUNK0_DONE", "status": "succeeded", "error": None},
            {"chunk_id": 1, "job_name": "jobs/CHUNK1_RUNNING", "status": "running", "error": None},
        ]
        state = {
            "upload_id": "resume-u1",
            "company_name": "Co",
            "pipeline": "gsearch",
            "status": "completed_with_errors",
            "rows": rows,
            "gemini_batch": {"status": "running", "chunks": existing_chunks},
        }
        persisted = {"state": state}

        create_calls = []

        def fake_create(model, items, display_name):
            create_calls.append([k for k, _ in items])
            return {"name": "jobs/NEW", "_keys": [k for k, _ in items]}

        def fake_get(name):
            # Both jobs return terminal-succeeded (chunk0 job should never be polled if skipped,
            # chunk1 job gets re-polled).
            return {"name": name, "done": True,
                    "state": {"name": "JOB_STATE_SUCCEEDED"},
                    "_keys": [f"row-{i}" for i in range(6, 11)]}

        def fake_collect(obj):
            out = []
            for k in (obj.get("_keys") or []):
                idx = int(k.split("-")[1])
                out.append({
                    "key": k,
                    "text": json.dumps({"official_website": f"https://c{idx}.com", "confidence_score": 90}),
                    "usage": {},
                })
            return out

        async def fake_persist(uid, st):
            persisted["state"] = st

        with mock.patch.dict("os.environ", {
                "GEMINI_BATCH_SHARD_SIZE": "5",
                "GEMINI_BATCH_MAX_INFLIGHT": "5",
                "GEMINI_API_KEY": "k",
                "GEMINI_BATCH_TIMEOUT_SEC": "3600",
                "GEMINI_BATCH_POLL_SEC": "5",
            }, clear=False), \
             mock.patch.object(app, "read_upload_artifact",
                               new=mock.AsyncMock(side_effect=lambda u, k: persisted["state"])), \
             mock.patch.object(app, "persist_upload_state", new=fake_persist), \
             mock.patch.object(app, "write_upload_text_artifact", new=mock.AsyncMock()), \
             mock.patch("app.services.ai_mode.gemini_batch.create_batch", side_effect=fake_create), \
             mock.patch("app.services.ai_mode.gemini_batch.get_batch", side_effect=fake_get), \
             mock.patch("app.services.ai_mode.gemini_batch.collect_results", side_effect=fake_collect):
            await app.run_gemini_batch_for_upload("resume-u1")

        # create_batch must NOT have been called for chunk 0 (it was already succeeded).
        # It should not be called at all for chunk 1 either, since chunk 1 already has a job_name.
        self.assertEqual(create_calls, [],
                         "create_batch should not be called when both chunks have existing job_names")

        gb = persisted["state"]["gemini_batch"]
        # Aggregate must be terminal
        self.assertIn(gb["status"], {"succeeded", "completed_with_errors", "failed"},
                      f"aggregate must be terminal, got {gb['status']!r}")

        # Chunk 0 must remain succeeded
        chunk0 = next(c for c in gb["chunks"] if c["chunk_id"] == 0)
        self.assertEqual(chunk0["status"], "succeeded")

        # Chunk 1 must now be succeeded (re-polled its existing job)
        chunk1 = next(c for c in gb["chunks"] if c["chunk_id"] == 1)
        self.assertEqual(chunk1["status"], "succeeded")

        # Both chunks succeeded => aggregate = succeeded
        self.assertEqual(gb["status"], "succeeded")

        # Chunk 0 rows: already had official_website set before the run -> must still have it
        rows_out = persisted["state"]["rows"]
        for r in rows_out[:5]:
            self.assertEqual(r["status"], "completed",
                             f"row {r['row_index']} (chunk0) should remain completed")
            self.assertTrue(r["result"].get("official_website"),
                            f"row {r['row_index']} (chunk0) should still have official_website")

        # Chunk 1 rows: were pending -> now resolved
        for r in rows_out[5:]:
            self.assertEqual(r["status"], "completed",
                             f"row {r['row_index']} (chunk1) should be completed after re-poll")
            self.assertTrue(r["result"].get("official_website"),
                            f"row {r['row_index']} (chunk1) should have official_website")


# ---------------------------------------------------------------------------
# Scenario 2: Batch timeout
# ---------------------------------------------------------------------------

class TestBatchTimeout(unittest.IsolatedAsyncioTestCase):
    """get_batch never returns terminal for chunk 1; GEMINI_BATCH_TIMEOUT_SEC=1.
    Chunk 1 must end up 'failed', its rows not-found. Chunk 0 succeeds ->
    aggregate = completed_with_errors."""

    async def test_timeout_marks_chunk_failed_and_aggregate_cwe(self):
        # 10 rows: chunk 0 (idx 1-5) and chunk 1 (idx 6-10)
        rows = [_pending_row(i) for i in range(1, 11)]
        state = {
            "upload_id": "timeout-u1",
            "company_name": "Co",
            "pipeline": "gsearch",
            "status": "completed_with_errors",
            "rows": rows,
            "gemini_batch": {"status": "queued", "chunks": []},
        }
        persisted = {"state": state}

        def fake_create(model, items, display_name):
            # Extract chunk_id from display_name (format: gsearch-{upload_id}-chunk{chunk_id})
            chunk_id = display_name.rsplit("chunk", 1)[-1]
            keys = [k for k, _ in items]
            return {"name": f"jobs/chunk-{chunk_id}", "_keys": keys}

        def fake_get(name):
            if name == "jobs/chunk-1":
                # Never returns terminal (simulates perpetually running job)
                return {"name": name, "done": False, "state": {"name": "JOB_STATE_RUNNING"}}
            # Chunk 0's job terminates immediately
            return {"name": name, "done": True,
                    "state": {"name": "JOB_STATE_SUCCEEDED"},
                    "_keys": [f"row-{i}" for i in range(1, 6)]}

        def fake_collect(obj):
            keys = obj.get("_keys") or []
            out = []
            for k in keys:
                idx = int(k.split("-")[1])
                out.append({
                    "key": k,
                    "text": json.dumps({"official_website": f"https://c{idx}.com", "confidence_score": 90}),
                    "usage": {},
                })
            return out

        async def fake_persist(uid, st):
            persisted["state"] = st

        # The driver uses max(60, _get_int_env("GEMINI_BATCH_TIMEOUT_SEC", 1800)) so the
        # minimum real wall-clock wait is 60s even with env set to 1. To avoid this,
        # we make asyncio.get_event_loop().time() return a value that jumps past the deadline
        # after the first non-terminal poll on chunk 1. That triggers the TimeoutError
        # code path without any real sleep.
        import asyncio as _asyncio

        real_loop = _asyncio.get_event_loop()
        real_loop_time = real_loop.time

        # After chunk 1 gets its first non-terminal response, bump the reported time
        # far beyond any deadline (deadline = time_at_creation + 60).
        get_batch_calls = {"n": 0}
        time_offset = {"v": 0.0}

        original_get_batch = fake_get

        def fake_get_with_bump(name):
            result = original_get_batch(name)
            if name == "jobs/chunk-1" and not result.get("done"):
                # After returning non-terminal, bump time so next deadline check fires.
                time_offset["v"] = 10000.0
            return result

        def patched_loop_time():
            return real_loop_time() + time_offset["v"]

        async def fast_sleep(delay):
            await _asyncio.sleep(0)

        with mock.patch.dict("os.environ", {
                "GEMINI_API_KEY": "k",
                # 10 rows / 5 per shard = the two chunks this test needs.
                "GEMINI_BATCH_SHARD_SIZE": "5",
                "GEMINI_BATCH_MAX_INFLIGHT": "5",
            }, clear=False), \
             mock.patch.object(app, "read_upload_artifact",
                               new=mock.AsyncMock(side_effect=lambda u, k: persisted["state"])), \
             mock.patch.object(app, "persist_upload_state", new=fake_persist), \
             mock.patch.object(app, "write_upload_text_artifact", new=mock.AsyncMock()), \
             mock.patch("app.services.ai_mode.gemini_batch.create_batch", side_effect=fake_create), \
             mock.patch("app.services.ai_mode.gemini_batch.get_batch", side_effect=fake_get_with_bump), \
             mock.patch("app.services.ai_mode.gemini_batch.collect_results", side_effect=fake_collect), \
             mock.patch("asyncio.sleep", side_effect=fast_sleep), \
             mock.patch.object(real_loop, "time", side_effect=patched_loop_time):
            await app.run_gemini_batch_for_upload("timeout-u1")

        gb = persisted["state"]["gemini_batch"]
        chunks_by_id = {c["chunk_id"]: c for c in gb["chunks"]}

        # Chunk 0 succeeded
        self.assertEqual(chunks_by_id[0]["status"], "succeeded",
                         "chunk 0 should have succeeded")

        # Chunk 1 must be failed (timed out)
        self.assertEqual(chunks_by_id[1]["status"], "failed",
                         "chunk 1 should have failed due to timeout")
        self.assertIsNotNone(chunks_by_id[1]["error"],
                             "chunk 1 must carry an error message")

        # Aggregate = completed_with_errors (one ok, one failed)
        self.assertEqual(gb["status"], "completed_with_errors",
                         f"aggregate should be completed_with_errors, got {gb['status']!r}")

        # Chunk 1 rows (idx 6-10) must be not-found
        rows_out = persisted["state"]["rows"]
        chunk1_rows = [r for r in rows_out if r["row_index"] in range(6, 11)]
        for r in chunk1_rows:
            self.assertEqual(r["status"], "failed",
                             f"row {r['row_index']} (chunk1/timed-out) must be failed")
            self.assertFalse(r["result"].get("official_website"),
                             f"row {r['row_index']} must not have official_website")

        # Chunk 0 rows (idx 1-5) must be resolved
        chunk0_rows = [r for r in rows_out if r["row_index"] in range(1, 6)]
        for r in chunk0_rows:
            self.assertEqual(r["status"], "completed",
                             f"row {r['row_index']} (chunk0) should be completed")
            self.assertTrue(r["result"].get("official_website"),
                            f"row {r['row_index']} (chunk0) should have official_website")


# ---------------------------------------------------------------------------
# Scenario 3: Partial output (mapping by key, not position)
# ---------------------------------------------------------------------------

class TestPartialOutput(unittest.IsolatedAsyncioTestCase):
    """collect_results returns records for a SUBSET of keys, and returns them
    out of order to prove the mapping is by key, not position."""

    async def test_partial_output_maps_by_key_not_position(self):
        # 5 rows: idx 10, 20, 30, 40, 50 (non-contiguous to expose position-vs-key bugs)
        idxs = [10, 20, 30, 40, 50]
        rows = [_pending_row(i) for i in idxs]
        state = {
            "upload_id": "partial-u1",
            "company_name": "Co",
            "pipeline": "gsearch",
            "status": "completed_with_errors",
            "rows": rows,
            "gemini_batch": {"status": "queued", "chunks": []},
        }
        persisted = {"state": state}

        def fake_create(model, items, display_name):
            keys = [k for k, _ in items]
            return {"name": "jobs/PARTIAL", "_keys": keys}

        def fake_get(name):
            return {"name": name, "done": True,
                    "state": {"name": "JOB_STATE_SUCCEEDED"},
                    "_keys": [f"row-{i}" for i in idxs]}

        # Return only rows 10, 30, 50 — out of order (50 first, then 10, then 30)
        # to prove the driver maps by key not by list position.
        def fake_collect(obj):
            return [
                {"key": "row-50", "text": json.dumps({"official_website": "https://c50.com"}), "usage": {}},
                {"key": "row-10", "text": json.dumps({"official_website": "https://c10.com"}), "usage": {}},
                {"key": "row-30", "text": json.dumps({"official_website": "https://c30.com"}), "usage": {}},
            ]

        async def fake_persist(uid, st):
            persisted["state"] = st

        with mock.patch.dict("os.environ", {
                "GEMINI_BATCH_SHARD_SIZE": "100",
                "GEMINI_BATCH_MAX_INFLIGHT": "5",
                "GEMINI_API_KEY": "k",
                "GEMINI_BATCH_TIMEOUT_SEC": "3600",
                "GEMINI_BATCH_POLL_SEC": "5",
            }, clear=False), \
             mock.patch.object(app, "read_upload_artifact",
                               new=mock.AsyncMock(side_effect=lambda u, k: persisted["state"])), \
             mock.patch.object(app, "persist_upload_state", new=fake_persist), \
             mock.patch.object(app, "write_upload_text_artifact", new=mock.AsyncMock()), \
             mock.patch("app.services.ai_mode.gemini_batch.create_batch", side_effect=fake_create), \
             mock.patch("app.services.ai_mode.gemini_batch.get_batch", side_effect=fake_get), \
             mock.patch("app.services.ai_mode.gemini_batch.collect_results", side_effect=fake_collect):
            await app.run_gemini_batch_for_upload("partial-u1")

        rows_out = persisted["state"]["rows"]
        by_idx = {r["row_index"]: r for r in rows_out}

        # Present keys: 10, 30, 50 — correct URL per key (NOT positional)
        for i in [10, 30, 50]:
            r = by_idx[i]
            self.assertEqual(r["status"], "completed",
                             f"row {i} was in collect_results — should be completed")
            self.assertEqual(r["result"].get("official_website"), f"https://c{i}.com",
                             f"row {i} official_website should be https://c{i}.com (by key, not position)")

        # Missing keys: 20, 40 — not-found
        for i in [20, 40]:
            r = by_idx[i]
            self.assertEqual(r["status"], "failed",
                             f"row {i} missing from collect_results — should be failed")
            self.assertFalse(r["result"].get("official_website"),
                             f"row {i} should have no official_website")

        # The chunk itself succeeded (collect_results succeeded; partial output is not a chunk failure)
        gb = persisted["state"]["gemini_batch"]
        self.assertEqual(gb["status"], "succeeded",
                         "aggregate should be succeeded (chunk ran; partial rows expected)")


# ---------------------------------------------------------------------------
# Scenario 4: Re-finalize after /retry-failed-rows
# ---------------------------------------------------------------------------

class TestReFinalizeAfterRetry(unittest.IsolatedAsyncioTestCase):
    """Simulate the /retry-failed-rows reset: gemini_batch set to waiting_for_rows
    and previously-failed rows reset to queued/completed-pending status.
    Running the driver again must re-chunk those rows and finalize."""

    async def test_refinalize_after_retry_failed_rows_reset(self):
        # Simulate post-retry state:
        # - 3 rows previously succeeded (have official_website)
        # - 2 rows had failed and were reset by /retry-failed-rows:
        #   their status is 'completed' (re-processed by worker, now pending Gemini again)
        # - gemini_batch was reset to waiting_for_rows by the endpoint

        done_rows = [_already_done_row(i) for i in range(1, 4)]   # rows 1, 2, 3 — already good
        retry_rows = [_pending_row(i) for i in range(4, 6)]        # rows 4, 5 — re-run by worker

        rows = done_rows + retry_rows
        state = {
            "upload_id": "retry-u1",
            "company_name": "Co",
            "pipeline": "gsearch",
            "status": "completed_with_errors",
            "rows": rows,
            # This is exactly what /retry-failed-rows sets when batch mode is on:
            "gemini_batch": {
                "status": "waiting_for_rows",
                "queued_at": None,
                "job_name": None,
                "error": None,
                "chunks": [],   # reset: prior chunk state is cleared
            },
        }
        persisted = {"state": state}

        # The driver is now re-invoked (called from maybe_start_gemini_batch_for_upload
        # after the retried rows complete processing). It should pick up ALL rows with
        # status in {completed, failed} and re-finalize.

        created = {"calls": []}

        def fake_create(model, items, display_name):
            keys = [k for k, _ in items]
            created["calls"].append(keys)
            return {"name": "jobs/RETRY_RUN", "_keys": keys}

        def fake_get(name):
            return {"name": name, "done": True,
                    "state": {"name": "JOB_STATE_SUCCEEDED"},
                    "_keys": created["calls"][-1] if created["calls"] else []}

        def fake_collect(obj):
            out = []
            for k in (obj.get("_keys") or []):
                idx = int(k.split("-")[1])
                out.append({
                    "key": k,
                    "text": json.dumps({"official_website": f"https://c{idx}.com", "confidence_score": 85}),
                    "usage": {},
                })
            return out

        finalize_calls = {"n": 0}
        original_persist = None

        async def fake_persist(uid, st):
            persisted["state"] = st

        with mock.patch.dict("os.environ", {
                "GEMINI_BATCH_SHARD_SIZE": "100",
                "GEMINI_BATCH_MAX_INFLIGHT": "5",
                "GEMINI_API_KEY": "k",
                "GEMINI_BATCH_TIMEOUT_SEC": "3600",
                "GEMINI_BATCH_POLL_SEC": "5",
            }, clear=False), \
             mock.patch.object(app, "read_upload_artifact",
                               new=mock.AsyncMock(side_effect=lambda u, k: persisted["state"])), \
             mock.patch.object(app, "persist_upload_state", new=fake_persist), \
             mock.patch.object(app, "write_upload_text_artifact", new=mock.AsyncMock()), \
             mock.patch("app.services.ai_mode.gemini_batch.create_batch", side_effect=fake_create), \
             mock.patch("app.services.ai_mode.gemini_batch.get_batch", side_effect=fake_get), \
             mock.patch("app.services.ai_mode.gemini_batch.collect_results", side_effect=fake_collect):
            await app.run_gemini_batch_for_upload("retry-u1")

        gb = persisted["state"]["gemini_batch"]

        # Aggregate must be terminal
        self.assertIn(gb["status"], {"succeeded", "completed_with_errors", "failed"},
                      f"aggregate must be terminal after re-finalize, got {gb['status']!r}")

        rows_out = persisted["state"]["rows"]
        by_idx = {r["row_index"]: r for r in rows_out}

        # Rows 1-3 (already done) must remain completed with official_website
        for i in range(1, 4):
            r = by_idx[i]
            self.assertTrue(r["result"].get("official_website"),
                            f"already-done row {i} must keep its official_website")
            self.assertEqual(r["status"], "completed",
                             f"already-done row {i} must remain completed")

        # Rows 4-5 (retried) must now be resolved via Gemini
        for i in range(4, 6):
            r = by_idx[i]
            self.assertEqual(r["status"], "completed",
                             f"retried row {i} should be completed after re-finalize")
            self.assertEqual(r["result"].get("official_website"), f"https://c{i}.com",
                             f"retried row {i} should have correct official_website")

        # The driver must have created exactly one batch job (for the 5 eligible rows)
        self.assertEqual(len(created["calls"]), 1,
                         "driver should create exactly one batch job for the reset rows")

        # The batch job should include ALL rows (done rows are included in _build_batch_items_for_state)
        submitted_keys = set(created["calls"][0])
        self.assertEqual(submitted_keys, {f"row-{i}" for i in range(1, 6)},
                         "all 5 rows should be re-submitted (including already-done ones)")


if __name__ == "__main__":
    unittest.main()
