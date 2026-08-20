import asyncio
import json
import unittest
from unittest import mock

from app.services.serpwow import engine as app


def _completed_row(idx, candidate="https://c%d.com"):
    url = candidate % idx
    return {"row_index": idx, "company_name": f"C{idx}", "country": "US", "status": "completed",
            "error": "Pending Gemini batch post-processing decision.",
            "result": {"official_website": None,
                       "context": {"candidates": [url], "cost_breakdown": {"serpwow_request_count": 1}}}}


class TestChunked(unittest.IsolatedAsyncioTestCase):
    async def test_three_chunks_one_fails(self):
        rows = [_completed_row(i) for i in range(1, 101)]  # 100 rows
        state = {"upload_id": "u1", "company_name": "Co", "pipeline": "gsearch",
                 "status": "completed_with_errors", "rows": rows,
                 "gemini_batch": {"status": "queued", "chunks": []}}
        persisted = {"state": state}

        async def fake_persist(uid, st):
            persisted["state"] = st

        # Each create returns a job_name encoding the chunk's keys; get_batch returns done;
        # collect_results returns a pick for chunks 0 and 2, FAILS chunk 1.
        created = {"n": 0, "display_names": []}

        def fake_create(model, items, display_name):
            created["n"] += 1
            created["display_names"].append(display_name)
            keys = [k for k, _ in items]
            if created["n"] == 2:  # second chunk submitted -> simulate a failed job
                return {"name": "jobs/FAIL", "_keys": keys}
            return {"name": f"jobs/OK{created['n']}", "_keys": keys}

        def fake_get(name):
            return {"name": name, "done": True,
                    "state": {"name": "JOB_STATE_FAILED" if name == "jobs/FAIL" else "JOB_STATE_SUCCEEDED"}}

        def fake_collect(obj):
            if obj.get("name") == "jobs/FAIL":
                return []
            # map every key to a pick
            out = []
            for k in obj.get("_keys", []):
                idx = int(k.split("-")[1])
                out.append({"key": k, "text": json.dumps({"official_website": f"https://c{idx}.com",
                                                            "confidence_score": 90}), "usage": {}})
            return out

        with mock.patch.dict("os.environ", {"GEMINI_BATCH_SHARD_SIZE": "40",
                                            "GEMINI_BATCH_MAX_INFLIGHT": "5",
                                            "GEMINI_API_KEY": "k"}, clear=False), \
             mock.patch.object(app, "read_upload_artifact", new=mock.AsyncMock(side_effect=lambda u, k: persisted["state"])), \
             mock.patch.object(app, "persist_upload_state", new=fake_persist), \
             mock.patch.object(app, "write_upload_text_artifact", new=mock.AsyncMock()), \
             mock.patch("app.services.ai_mode.gemini_batch.create_batch", side_effect=fake_create), \
             mock.patch("app.services.ai_mode.gemini_batch.get_batch", side_effect=fake_get), \
             mock.patch("app.services.ai_mode.gemini_batch.collect_results", side_effect=fake_collect):
            await app.run_gemini_batch_for_upload("u1")

        gb = persisted["state"]["gemini_batch"]
        self.assertEqual(len(gb["chunks"]), 3)            # 40/40/20
        self.assertEqual(gb["status"], "completed_with_errors")
        statuses = {c["status"] for c in gb["chunks"]}
        self.assertEqual(statuses, {"succeeded", "failed"})
        self.assertEqual(created["display_names"], [
            "gsearch-u1-gen0-chunk0",
            "gsearch-u1-gen0-chunk1",
            "gsearch-u1-gen0-chunk2",
        ])
        # The failed chunk's rows are not-found; succeeded chunks' rows have a URL.
        # Chunk 0 (40 rows) succeeds, chunk 1 (40 rows) fails, chunk 2 (20 rows) succeeds.
        # 40 + 20 = 60 rows should have URLs.
        rows = persisted["state"]["rows"]
        found = [r for r in rows if (r["result"].get("official_website"))]
        self.assertEqual(len(found), 60)

    def test_batch_postprocess_pending_returns_false_on_terminal(self):
        """_batch_postprocess_pending returns False once gemini_batch.status is terminal."""
        state = {"pipeline": "gsearch",
                 "gemini_batch": {"status": "completed_with_errors"}}
        with mock.patch.object(app, "_batch_postprocess_enabled_for", return_value=True):
            result = app._batch_postprocess_pending(state)
        self.assertFalse(result)

    def test_apply_batch_parsed_to_row_summary_parity(self):
        """summary field should always be str, defaulting to '' when absent."""
        # Test 1: parsed has no summary, row has no summary -> should be ""
        row = {"result": {}, "company_name": "TestCo", "country": "US"}
        parsed = {"official_website": "https://example.com"}
        usage = {}
        app._apply_batch_parsed_to_row(row, parsed, usage, "gemini-2.0-flash-001")
        self.assertEqual(row["result"]["summary"], "")
        self.assertIsInstance(row["result"]["summary"], str)

        # Test 2: parsed has non-string truthy value -> should be str-coerced
        row = {"result": {}, "company_name": "TestCo", "country": "US"}
        parsed = {"official_website": "https://example.com", "summary": 123}
        usage = {}
        app._apply_batch_parsed_to_row(row, parsed, usage, "gemini-2.0-flash-001")
        self.assertEqual(row["result"]["summary"], "123")
        self.assertIsInstance(row["result"]["summary"], str)

        # Test 3: row had prior summary, parsed has no summary -> preserve prior
        row = {"result": {"summary": "Prior summary text"}, "company_name": "TestCo", "country": "US"}
        parsed = {"official_website": "https://example.com"}
        usage = {}
        app._apply_batch_parsed_to_row(row, parsed, usage, "gemini-2.0-flash-001")
        self.assertEqual(row["result"]["summary"], "Prior summary text")

        # Test 4: parsed has string summary -> use it
        row = {"result": {}, "company_name": "TestCo", "country": "US"}
        parsed = {"official_website": "https://example.com", "summary": "New summary"}
        usage = {}
        app._apply_batch_parsed_to_row(row, parsed, usage, "gemini-2.0-flash-001")
        self.assertEqual(row["result"]["summary"], "New summary")


if __name__ == "__main__":
    unittest.main()
