import asyncio
import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.services.serpwow import engine

CSV = (
    "Input_URL,Company_Name_X,Box_No,Image_URL,Company_Name_Y,OCR_Status\n"
    "https://www.eastlinkcap.com/p,eastlinkcap,2.0,img2,Modal,SUCCESS\n"
    "https://www.eastlinkcap.com/p,eastlinkcap,3.0,img3,Modal,SUCCESS\n"
).encode()

FAKE_ENV = {"GEMINI_API_KEY": "k", "SERPWOW_API_KEY": "k",
            "RELATIONSHIP_LLM_BATCH": "false", "S3_BUCKET": ""}


class TestRelationshipUploadEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(engine.app)

    def _post(self, data=None, csv_bytes=CSV):
        return self.client.post(
            "/uploads/relationship",
            files={"file": ("rel.csv", io.BytesIO(csv_bytes), "text/csv")},
            data=data or {"company_id": "c1", "company_name": "Acme"},
        )

    def _happy_patches(self):
        svc = MagicMock()
        svc.get_company.return_value = {"id": "c1", "name": "Acme"}
        svc.create_run.return_value = "run-db-1"
        return [
            patch.dict("os.environ", FAKE_ENV),
            patch("app.services.companies.get_company_service", return_value=svc),
            patch.object(engine, "rabbitmq_exchange", MagicMock()),
            patch.object(engine, "publish_job", AsyncMock()),
            patch.object(engine, "persist_upload_state", AsyncMock()),
            patch.object(engine, "_upload_dir", MagicMock()),
        ]

    def test_happy_path_processes_every_row_no_dedup(self):
        patches = self._happy_patches()
        for p in patches:
            p.start()
        try:
            resp = self._post()
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            self.assertEqual(body["total_rows"], 2)      # no dedup: 2 rows in → 2 out
            self.assertEqual(body["blank_rows"], 0)
            self.assertEqual(body["unique_pairs"], 2)
            # state carries the relationship block + pair-row extras
            state = engine.persist_upload_state.call_args[0][1]
            self.assertEqual(state["pipeline"], "relationship")
            self.assertEqual(state["relationship"]["blank_rows"], 0)
            self.assertEqual(state["relationship"]["row_count_original"], 2)
            self.assertEqual(len(state["rows"]), 2)
            row = state["rows"][0]
            self.assertEqual(row["company_name"], "Modal")
            self.assertEqual(row["x_name"], "eastlinkcap")
            self.assertEqual(row["source_row_indices"], [0])
            # job carries the same extras
            job = engine.publish_job.call_args[0][0]
            self.assertEqual(job["x_name"], "eastlinkcap")
            self.assertEqual(job["input_url"], "https://www.eastlinkcap.com/p")
            self.assertEqual(job["pipeline"], "relationship")
            self.assertNotIn("source_row_indices", job)
        finally:
            for p in patches:
                p.stop()

    def test_missing_y_column_is_400(self):
        patches = self._happy_patches()
        for p in patches:
            p.start()
        try:
            resp = self._post(csv_bytes=b"Company_Name_X,Other\na,b\n")
            self.assertEqual(resp.status_code, 400)
            self.assertIn("Company_Name_Y", resp.json()["detail"])
        finally:
            for p in patches:
                p.stop()

    def test_missing_gemini_key_is_400(self):
        patches = self._happy_patches()
        for p in patches:
            p.start()
        try:
            with patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
                resp = self._post()
            self.assertEqual(resp.status_code, 400)
            self.assertIn("GEMINI_API_KEY", resp.json()["detail"])
        finally:
            for p in patches:
                p.stop()

    def test_blank_required_value_is_400(self):
        patches = self._happy_patches()
        for p in patches:
            p.start()
        try:
            resp = self._post(csv_bytes=(
                b"Input_URL,Company_Name_X,Company_Name_Y\n"
                b"https://m25vc.com/p,m25vc,\n"
            ))
            self.assertEqual(resp.status_code, 400)
            self.assertIn("Company_Name_Y", resp.json()["detail"])
        finally:
            for p in patches:
                p.stop()


class TestRetryFailedRowsCarriesPairFields(unittest.TestCase):
    """Regression: a retried relationship row must carry x_name/input_url/city
    (dropped previously) so it doesn't hit the not-has_x short-circuit again,
    but must NOT carry source_row_indices (fan-out bookkeeping, not a job field)."""

    def setUp(self):
        self.client = TestClient(engine.app)

    def _state(self):
        return {
            "upload_id": "u-retry-1",
            "company_name": "Acme",
            "pipeline": "relationship",
            "phase": "all",
            "rows": [
                {
                    "row_index": 1,
                    "company_name": "Modal",
                    "country": "",
                    "status": "failed",
                    "error": "no_company_x",
                    "x_name": "eastlinkcap",
                    "input_url": "https://www.eastlinkcap.com/p",
                    "city": "Boston",
                    "source_row_indices": [1, 2],
                }
            ],
        }

    def test_retry_republishes_pair_fields_without_source_row_indices(self):
        state = self._state()
        with patch.object(engine, "rabbitmq_exchange", MagicMock()), \
             patch.object(engine, "get_upload_state", AsyncMock(return_value=state)), \
             patch.object(engine, "read_upload_artifact", AsyncMock(return_value=state)), \
             patch.object(engine, "persist_upload_state", AsyncMock()), \
             patch.object(engine, "publish_job", AsyncMock()) as pub:
            resp = self.client.post("/uploads/u-retry-1/retry-failed-rows")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(pub.await_count, 1)
        job = pub.call_args[0][0]
        self.assertEqual(job["x_name"], "eastlinkcap")
        self.assertEqual(job["input_url"], "https://www.eastlinkcap.com/p")
        self.assertEqual(job["city"], "Boston")
        self.assertNotIn("source_row_indices", job)


class TestWorkerDispatchAndPendingMark(unittest.TestCase):
    def _job(self):
        return {"upload_id": "u1", "row_index": 1, "company_name": "Modal",
                "country": "", "pipeline": "relationship", "phase": "all",
                "x_name": "eastlinkcap", "input_url": "https://www.eastlinkcap.com/p",
                "city": "", "uploaded_at": "now", "upload_company_name": "Acme"}

    def _run_job(self, crawl_resp, env):
        async def scenario():
            with patch.object(engine, "get_upload_state",
                              AsyncMock(return_value={"rows": []})), \
                 patch.object(engine, "update_row_state", AsyncMock()) as urs, \
                 patch.object(engine, "upload_serpwow_json_to_s3",
                              AsyncMock(return_value=(None, None))), \
                 patch.object(engine, "execute_relationship_lookup_for_worker",
                              AsyncMock(return_value=(crawl_resp, "{}"))) as exe, \
                 patch.dict("os.environ", env):
                await engine.process_upload_job(self._job())
            return exe, urs
        return asyncio.run(scenario())

    def _crawl(self, official, skip_llm=False, row_error=None):
        from app.services.serpwow.schemas import CrawlResponse
        return CrawlResponse(
            company_name="Modal", country="", firm_id=None, input_industry=None,
            input_full_address=None, official_website=official, summary="s",
            address=None, phone=None, email=None, industry=None,
            products=[], services=[],
            website_company_descirption_ai=None,
            website_company_descirption_translated_ai=None,
            massive_proxy_cost_usd=0.0, serpwow_cost_usd=0.02,
            gemini_cost_usd=0.0, total_cost_usd=0.02,
            context={"pipeline": "relationship", "skip_llm": skip_llm,
                     "row_error": row_error, "candidates": [],
                     "cost_breakdown": {"serpwow_request_count": 1}})

    def test_dispatches_to_relationship_executor_with_pair_fields(self):
        exe, urs = self._run_job(self._crawl("https://modal.com/"),
                                 {"RELATIONSHIP_LLM_BATCH": "false"})
        kwargs = exe.call_args.kwargs
        self.assertEqual(kwargs["y_name"], "Modal")
        self.assertEqual(kwargs["x_name"], "eastlinkcap")
        self.assertEqual(kwargs["input_url"], "https://www.eastlinkcap.com/p")
        final = urs.call_args_list[-1]
        self.assertEqual(final.kwargs["status"], "completed")

    def test_batch_mode_keeps_evidence_rows_pending(self):
        exe, urs = self._run_job(self._crawl(None),
                                 {"RELATIONSHIP_LLM_BATCH": "true"})
        final = urs.call_args_list[-1]
        self.assertEqual(final.kwargs["status"], "completed")
        self.assertIn("Pending Gemini batch", final.kwargs["error"])

    def test_batch_mode_fails_skip_llm_rows_immediately(self):
        # relationship is a REPORTING_PIPELINES member: a business "not found" sentinel
        # (skip_llm short-circuits the batch-pending branch) remaps to completed/not_found,
        # not the legacy binary "failed".
        from app.services.serpwow.constants import REL_ERROR_NO_EVIDENCE
        exe, urs = self._run_job(
            self._crawl(None, skip_llm=True, row_error=REL_ERROR_NO_EVIDENCE),
            {"RELATIONSHIP_LLM_BATCH": "true"})
        final = urs.call_args_list[-1]
        self.assertEqual(final.kwargs["status"], "completed")
        self.assertEqual(final.kwargs["error"], REL_ERROR_NO_EVIDENCE)
        self.assertEqual(final.kwargs["outcome"], "not_found")

    def test_per_row_mode_uses_context_row_error(self):
        # Same gated remap: not-confirmed relationship is a not_found outcome, and
        # relationship is in scope for the remap -> row status is completed, not failed.
        from app.services.serpwow.constants import REL_ERROR_NOT_CONFIRMED
        exe, urs = self._run_job(
            self._crawl(None, row_error=REL_ERROR_NOT_CONFIRMED),
            {"RELATIONSHIP_LLM_BATCH": "false"})
        final = urs.call_args_list[-1]
        self.assertEqual(final.kwargs["status"], "completed")
        self.assertEqual(final.kwargs["error"], REL_ERROR_NOT_CONFIRMED)
        self.assertEqual(final.kwargs["outcome"], "not_found")


if __name__ == "__main__":
    unittest.main()


class TestRelationshipPreviewEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(engine.app)

    def _preview(self, csv_bytes=CSV):
        return self.client.post(
            "/uploads/relationship/preview",
            files={"file": ("rel.csv", io.BytesIO(csv_bytes), "text/csv")},
        )

    def test_preview_counts_columns_and_sample(self):
        resp = self._preview()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["total_rows"], 2)      # no dedup: every row counted
        self.assertTrue(body["relationship"])
        self.assertNotIn("unique_pairs", body)
        self.assertEqual(body["columns_detected"]["company_name_y"], "Company_Name_Y")
        self.assertEqual(body["columns_detected"]["input_url"], "Input_URL")
        self.assertIsNone(body["columns_detected"]["city"])
        self.assertEqual(len(body["sample_rows"]), 2)
        sample = body["sample_rows"][0]
        self.assertEqual(sample["company_name_x"], "eastlinkcap")
        self.assertEqual(sample["company_name_y"], "Modal")
        self.assertNotIn("csv_rows", sample)
        self.assertEqual(body["sample_columns"][0], "company_name_x")
        self.assertEqual(body["warnings"], [])       # no duplicate warning anymore

    def test_preview_missing_x_column_is_400(self):
        resp = self._preview(
            b"Input_URL,Company_Name_Y,Other\nhttps://m25vc.com/p,Sanzo,z\n")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Company_Name_X", resp.json()["detail"])

    def test_preview_blank_required_value_is_400(self):
        resp = self._preview(
            b"Input_URL,Company_Name_X,Company_Name_Y\nhttps://m25vc.com/p,m25vc,\n")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Company_Name_Y", resp.json()["detail"])
