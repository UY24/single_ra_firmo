"""Offline tests for firmographics Gemini BATCH mode.

Toggle RESOLUTION is tested in test_llm_batch_config.py, next to the shared resolver.

Drives the real chunk engine (``engine.run_gemini_batch_for_upload``) with the Gemini Batch
API mocked, the same way test_gsearch_chunked does.
"""
import asyncio
import json
import os
import unittest
from unittest import mock

import httpx

from app.services.serpwow import engine as app
from app.services.serpwow import scrapedo_search_client as sc
from app.services.serpwow.modes import firmographics as fm

FIELDS = {"address": "Bertha-Benz-Str. 2", "phone": "+49 7142 9930-0",
          "email": "info@acme.com", "industry": "Manufacturing",
          "products": ["Towbars"], "services": ["OE development"]}


class ExecutorSkipsInlineCallTests(unittest.TestCase):
    def _row(self, overview):
        def handler(request):
            return httpx.Response(200, json={"ai_overview": overview} if overview else {})
        mc = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        orig = sc.search_with_ai_overview

        async def patched(q, gl="us", client=None):
            return await orig(q, gl, client=mc)

        async def go():
            with mock.patch.object(sc, "search_with_ai_overview", patched):
                return await fm.execute_firmographic_extraction("acme.com", "Acme", "DE")
        try:
            return asyncio.run(go())
        finally:
            asyncio.run(mc.aclose())

    def test_inline_gemini_is_not_called_in_batch_mode(self) -> None:
        """Paying inline AND batching would bill every row twice."""
        called = []

        def spy(*a, **k):
            called.append(1)
            return (dict(FIELDS), None, "m", {})

        with mock.patch.dict(os.environ, {"SCRAPEDO_TOKEN": "t",
                                          "LLM_BATCH": "true"}), \
                mock.patch.object(fm, "standardize_ai_overview_with_gemini", spy):
            resp, _ = self._row({"state": "complete", "text_blocks": [{"snippet": "x"}]})
        self.assertEqual(called, [], "inline Gemini ran despite batch mode")
        self.assertTrue(resp.context["mapping_ai"]["deferred_to_batch"])
        # No fields yet — the batch fills them.
        self.assertIsNone(resp.address)
        self.assertEqual(resp.gemini_cost_usd, 0.0)
        # Still only the search's credits.
        self.assertEqual(resp.context["cost_breakdown"]["scrapedo_credits"], 10)

    def test_inline_gemini_runs_when_batch_is_off(self) -> None:
        called = []

        def spy(*a, **k):
            called.append(1)
            return (dict(FIELDS), None, "m", {"promptTokenCount": 10,
                                              "candidatesTokenCount": 5})

        with mock.patch.dict(os.environ, {"SCRAPEDO_TOKEN": "t",
                                          "LLM_BATCH": "false"}), \
                mock.patch.object(fm, "standardize_ai_overview_with_gemini", spy):
            resp, _ = self._row({"state": "complete", "text_blocks": [{"snippet": "x"}]})
        self.assertEqual(called, [1])
        self.assertEqual(resp.address, FIELDS["address"])


def _batch_row(idx, overview=True):
    """A row as the worker leaves it in batch mode: scraped, not yet normalised."""
    scrapedo = {"provider": "scrapedo", "used": True, "domain": f"c{idx}.com",
                "ai_overview": ({"state": "complete", "text_blocks": [{"snippet": f"co {idx}"}]}
                                if overview else None),
                "billed_no_overview": not overview, "deferred": False,
                "search_requests": 1, "search_successful": 1,
                "ai_overview_requests": 0, "ai_overview_successful": 0,
                "request_count": 1, "successful_requests": 1, "failed_requests": 0,
                "credits": 10, "error": None}
    return {"row_index": idx, "company_name": f"C{idx}", "country": "DE",
            "status": "completed", "outcome": "not_found", "error": None,
            "result": {"official_website": f"https://c{idx}.com",
                       "gemini_cost_usd": 0.0, "total_cost_usd": 0.0,
                       "context": {"pipeline": "firmographics", "scrapedo": scrapedo,
                                   "cost_breakdown": {"scrapedo_requests": 1,
                                                      "scrapedo_credits": 10,
                                                      "scrapedo_search_successful": 1}}}}


class BatchRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_fills_fields_and_sets_outcomes(self) -> None:
        # 3 rows with an overview + 1 without: the one without must never be submitted,
        # because a paid request over a missing overview can only come back empty.
        rows = [_batch_row(i) for i in (1, 2, 3)] + [_batch_row(4, overview=False)]
        state = {"upload_id": "u1", "company_name": "Co", "pipeline": "firmographics",
                 "status": "completed", "rows": rows,
                 "gemini_batch": {"status": "queued", "chunks": []}}
        persisted = {"state": state}
        submitted_keys = []
        prompts = []

        async def fake_persist(uid, st):
            persisted["state"] = st

        def fake_create(model, items, display_name):
            keys = [k for k, _ in items]
            submitted_keys.extend(keys)
            prompts.extend(
                req["contents"][0]["parts"][0]["text"] for _, req in items)
            return {"name": "jobs/OK", "_keys": keys}

        def fake_get(name):
            return {"name": name, "done": True,
                    "state": {"name": "JOB_STATE_SUCCEEDED"}}

        def fake_collect(obj):
            out = []
            for k in obj.get("_keys", []):
                out.append({"key": k, "text": json.dumps(FIELDS),
                            "usage": {"promptTokenCount": 100,
                                      "candidatesTokenCount": 50}})
            return out

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k",
                                          "LLM_BATCH": "true"}), \
                mock.patch.object(app, "read_upload_artifact",
                                  new=mock.AsyncMock(side_effect=lambda u, k: persisted["state"])), \
                mock.patch.object(app, "persist_upload_state", new=fake_persist), \
                mock.patch.object(app, "write_upload_text_artifact", new=mock.AsyncMock()), \
                mock.patch("app.services.ai_mode.gemini_batch.create_batch", side_effect=fake_create), \
                mock.patch("app.services.ai_mode.gemini_batch.get_batch", side_effect=fake_get), \
                mock.patch("app.services.ai_mode.gemini_batch.collect_results", side_effect=fake_collect):
            await app.run_gemini_batch_for_upload("u1")

        # Row 4 had no overview -> not submitted, so no credits wasted on it.
        self.assertEqual(sorted(submitted_keys), ["row-1", "row-2", "row-3"])
        # The batch prompt is the firmographics normalisation prompt, not gsearch's resolver.
        self.assertIn("Convert the provided Google AI Overview", prompts[0])
        self.assertNotIn("company website resolver", prompts[0])

        out = {r["row_index"]: r for r in persisted["state"]["rows"]}
        for idx in (1, 2, 3):
            self.assertEqual(out[idx]["result"]["address"], FIELDS["address"])
            self.assertEqual(out[idx]["result"]["products"], FIELDS["products"])
            # Fields arrived -> found. NOT decided by official_website, which is the input.
            self.assertEqual(out[idx]["outcome"], "found")
            self.assertEqual(out[idx]["status"], "completed")
            # Batch tokens are billed to the row.
            self.assertGreater(out[idx]["result"]["gemini_cost_usd"], 0.0)
            # total_usd stays LLM-only: credits are not dollars.
            self.assertEqual(out[idx]["result"]["total_cost_usd"],
                             out[idx]["result"]["gemini_cost_usd"])
        # The unsubmitted row keeps its business not-found and spends nothing.
        self.assertEqual(out[4]["outcome"], "not_found")
        self.assertEqual(out[4]["result"]["gemini_cost_usd"], 0.0)
        self.assertEqual(persisted["state"]["gemini_batch"]["status"], "succeeded")

    async def test_batch_returning_nothing_leaves_a_not_found(self) -> None:
        """An empty batch answer must not become "found" off the echoed input URL."""
        rows = [_batch_row(1)]
        state = {"upload_id": "u2", "company_name": "Co", "pipeline": "firmographics",
                 "status": "completed", "rows": rows,
                 "gemini_batch": {"status": "queued", "chunks": []}}
        persisted = {"state": state}

        async def fake_persist(uid, st):
            persisted["state"] = st

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k",
                                          "LLM_BATCH": "true"}), \
                mock.patch.object(app, "read_upload_artifact",
                                  new=mock.AsyncMock(side_effect=lambda u, k: persisted["state"])), \
                mock.patch.object(app, "persist_upload_state", new=fake_persist), \
                mock.patch.object(app, "write_upload_text_artifact", new=mock.AsyncMock()), \
                mock.patch("app.services.ai_mode.gemini_batch.create_batch",
                           side_effect=lambda model, items, display_name: {
                               "name": "jobs/OK", "_keys": [k for k, _ in items]}), \
                mock.patch("app.services.ai_mode.gemini_batch.get_batch",
                           side_effect=lambda n: {"name": n, "done": True,
                                                  "state": {"name": "JOB_STATE_SUCCEEDED"}}), \
                mock.patch("app.services.ai_mode.gemini_batch.collect_results",
                           side_effect=lambda o: [{"key": k, "text": json.dumps(
                               {"address": None, "phone": None, "email": None,
                                "industry": None, "products": [], "services": []}),
                               "usage": {}} for k in o.get("_keys", [])]):
            await app.run_gemini_batch_for_upload("u2")

        # Guard against passing for the wrong reason: the job must have run and succeeded,
        # so "not_found" is the verdict on an EMPTY answer, not the fallout of a failure.
        self.assertEqual(persisted["state"]["gemini_batch"]["status"], "succeeded")
        row = persisted["state"]["rows"][0]
        self.assertEqual(row["outcome"], "not_found")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["error"], "Official website not found after Gemini batch post-processing.")


if __name__ == "__main__":
    unittest.main()
