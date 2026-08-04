"""Relationship upload/status endpoints after the scrape.do migration."""
import io
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.services.serpwow import relationship_store as store
from app.services.serpwow.engine import app
from tests.test_relationship_store import FakeS3, _patched

GOOD_CSV = (b"Input_URL,Company_Name_X,Company_Name_Y\n"
            b"https://acme.com/p,Acme,Sanzo\n")
ENV = {"GEMINI_API_KEY": "k", "SCRAPEDO_TOKEN": "t", "S3_BUCKET": "b"}


class UploadValidationTests(unittest.TestCase):
    def test_missing_scrapedo_token_is_a_400(self) -> None:
        with mock.patch.dict(os.environ, {**ENV, "SCRAPEDO_TOKEN": ""}, clear=False):
            client = TestClient(app)
            r = client.post("/uploads/relationship",
                            files={"file": ("in.csv", GOOD_CSV, "text/csv")},
                            data={"company_id": "c1", "company_name": "Acme"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("SCRAPEDO_TOKEN", r.json()["detail"])

    def test_serpwow_api_key_is_no_longer_required(self) -> None:
        """The relationship pipeline no longer calls SerpWow at all."""
        fake = FakeS3()
        with mock.patch.dict(os.environ, {**ENV, "SERPWOW_API_KEY": ""}, clear=False), \
                _patched(fake), \
                mock.patch("app.services.serpwow.engine.publish_relationship_run",
                           new=mock.AsyncMock()):
            client = TestClient(app)
            r = client.post("/uploads/relationship",
                            files={"file": ("in.csv", GOOD_CSV, "text/csv")},
                            data={"company_id": "c1", "company_name": "Acme"})
        self.assertEqual(r.status_code, 200)

    def test_a_bad_csv_reports_the_csv_problem_not_the_token(self) -> None:
        with mock.patch.dict(os.environ, {**ENV, "SCRAPEDO_TOKEN": ""}, clear=False):
            client = TestClient(app)
            r = client.post("/uploads/relationship",
                            files={"file": ("in.csv", b"nope\n1\n", "text/csv")},
                            data={"company_id": "c1", "company_name": "Acme"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Company_Name_Y", r.json()["detail"])


class UploadSideEffectTests(unittest.TestCase):
    def test_upload_writes_input_csv_pointer_and_status_then_publishes(self) -> None:
        fake = FakeS3()
        published = mock.AsyncMock()
        with mock.patch.dict(os.environ, ENV, clear=False), _patched(fake), \
                mock.patch("app.services.serpwow.engine.publish_relationship_run",
                           new=published):
            client = TestClient(app)
            r = client.post("/uploads/relationship",
                            files={"file": ("in.csv", GOOD_CSV, "text/csv")},
                            data={"company_id": "c1", "company_name": "Acme"})

        run_id = r.json()["upload_id"]
        with _patched(fake):
            pointer = store.read_run_pointer(run_id)
            status = store.read_status(pointer["prefix"])
            self.assertEqual(store.get_bytes(store.input_key(pointer["prefix"])),
                             GOOD_CSV)
        self.assertEqual(status["rows_total"], 1)
        self.assertEqual(status["phase"], "queued")
        published.assert_awaited_once()

    def test_supabase_run_db_id_rides_in_the_pointer(self) -> None:
        """Phase 3 has no state dict — the pointer is where it finds the runs row."""
        fake = FakeS3()
        svc = mock.MagicMock()
        svc.create_run.return_value = "run-db-7"
        with mock.patch.dict(os.environ, ENV, clear=False), _patched(fake), \
                mock.patch("app.services.companies.get_company_service",
                           return_value=svc), \
                mock.patch("app.services.serpwow.engine.publish_relationship_run",
                           new=mock.AsyncMock()):
            r = TestClient(app).post(
                "/uploads/relationship",
                files={"file": ("in.csv", GOOD_CSV, "text/csv")},
                data={"company_id": "c1", "company_name": "Acme"})
            with _patched(fake):
                pointer = store.read_run_pointer(r.json()["upload_id"])
        self.assertEqual(pointer["run_db_id"], "run-db-7")
        self.assertEqual(svc.create_run.call_args.kwargs["pipeline"], "relationship")
        self.assertEqual(svc.create_run.call_args.kwargs["total_rows"], 1)

    def test_no_state_json_object_is_ever_written(self) -> None:
        """state.json is what this migration removed; its return would be a regression."""
        fake = FakeS3()
        with mock.patch.dict(os.environ, ENV, clear=False), _patched(fake), \
                mock.patch("app.services.serpwow.engine.publish_relationship_run",
                           new=mock.AsyncMock()):
            TestClient(app).post(
                "/uploads/relationship",
                files={"file": ("in.csv", GOOD_CSV, "text/csv")},
                data={"company_id": "c1", "company_name": "Acme"})
        self.assertFalse(any(k.endswith("/state.json") for k in fake.objects))


class StatusTests(unittest.TestCase):
    def test_status_is_served_from_counters_not_from_rows(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            store.write_run_pointer("run1", "acme/relationship/run1", "Acme")
            store.put_object(store.status_key("acme/relationship/run1"), {
                "rows_total": 500000, "rows_scraped": 1234, "rows_failed": 2,
                "rows_billed_empty": 5, "credits": 12340, "requests": 1240,
                "phase": "scraping", "updated_at": "2026-08-04T00:00:00Z"})

            r = TestClient(app).get("/uploads/run1/status")

        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["pipeline"], "relationship")
        self.assertEqual(body["total_rows"], 500000)
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["serpwow_summary"]["cost"]["scrapedo_credits"], 12340)

    def test_an_unknown_run_id_still_404s(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            r = TestClient(app).get("/uploads/nope/status")
        self.assertEqual(r.status_code, 404)


class FileStopAndRerunTests(unittest.TestCase):
    """The /result, /stop and /retry-failed-rows relationship branches, all keyed
    off the run pointer so non-relationship ids keep their old behaviour."""

    PREFIX = "acme/relationship/run2"

    def _seeded(self) -> FakeS3:
        fake = FakeS3()
        with _patched(fake):
            store.write_run_pointer("run2", self.PREFIX, "Acme")
        return fake

    def test_result_serves_an_output_object_from_s3(self) -> None:
        fake = self._seeded()
        with _patched(fake):
            store.put_bytes(f"{self.PREFIX}/confirmed_relation.csv", b"a,b\n1,2\n")
            r = TestClient(app).get(
                "/uploads/run2/result", params={"file": "confirmed_relation.csv"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"a,b\n1,2\n")

    def test_result_404s_before_the_file_exists_and_400s_off_the_allowlist(self) -> None:
        fake = self._seeded()
        with _patched(fake):
            client = TestClient(app)
            missing = client.get("/uploads/run2/result", params={"file": "run.log"})
            bad = client.get("/uploads/run2/result", params={"file": "state.json"})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(bad.status_code, 400)

    def test_stop_writes_the_stop_marker(self) -> None:
        fake = self._seeded()
        with _patched(fake):
            r = TestClient(app).post("/uploads/run2/stop")
            self.assertTrue(store.stop_requested(self.PREFIX))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["stop_requested"])

    def test_rerun_failed_drops_error_markers_clears_stop_and_republishes(self) -> None:
        fake = self._seeded()
        published = mock.AsyncMock()
        with _patched(fake):
            store.put_object(store.error_key(self.PREFIX, 3), {"error": "boom"})
            store.put_object(store.raw_key(self.PREFIX, 4), {"ok": True})
            store.request_stop(self.PREFIX)
        with _patched(fake), mock.patch(
                "app.services.serpwow.engine.publish_relationship_run", new=published):
            r = TestClient(app).post("/uploads/run2/retry-failed-rows")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["retried_rows"], 1)
        self.assertNotIn(store.error_key(self.PREFIX, 3), fake.objects)
        # A successful row is never re-scraped, and the stop marker is lifted.
        self.assertIn(store.raw_key(self.PREFIX, 4), fake.objects)
        self.assertNotIn(store.stop_key(self.PREFIX), fake.objects)
        published.assert_awaited_once_with("run2")


PREVIEW_CSV = (
    "Input_URL,Company_Name_X,Box_No,Image_URL,Company_Name_Y,OCR_Status\n"
    "https://www.eastlinkcap.com/p,eastlinkcap,2.0,img2,Modal,SUCCESS\n"
    "https://www.eastlinkcap.com/p,eastlinkcap,3.0,img3,Modal,SUCCESS\n"
).encode()


class TestRelationshipPreviewEndpoint(unittest.TestCase):
    """The dry-run preview is unchanged by the scrape.do migration — it never
    touched SerpWow, state.json or the queue."""

    def setUp(self):
        self.client = TestClient(app)

    def _preview(self, csv_bytes=PREVIEW_CSV):
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


if __name__ == "__main__":
    unittest.main()
