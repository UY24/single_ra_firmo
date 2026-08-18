"""Relationship upload/status endpoints after the scrape.do migration."""
import io
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.services.serpwow import s3_run_store as store
from app.services.serpwow import engine
from app.services.serpwow.engine import app
from tests.test_s3_run_store import FakeS3, _patched

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

    def test_missing_s3_bucket_is_a_400_not_an_opaque_500(self) -> None:
        """This pipeline is S3-only — an unset bucket must fail like any other env key,
        not blow up inside the first put_bytes."""
        with mock.patch.dict(os.environ, {**ENV, "S3_BUCKET": ""}, clear=False):
            client = TestClient(app)
            r = client.post("/uploads/relationship",
                            files={"file": ("in.csv", GOOD_CSV, "text/csv")},
                            data={"company_id": "c1", "company_name": "Acme"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("S3_BUCKET", r.json()["detail"])

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

    def test_upload_still_succeeds_when_publishing_fails(self) -> None:
        """publish_relationship_run's own docstring says failure is tolerated, and the
        re-drive scan is the safety net — but it only tolerated `exchange is None`. A
        wait_for timeout or any AMQP error propagated, 500ing the user with no upload_id
        AFTER the run was fully created in S3, and 300s later the scan started spending
        money on a run the user believes never existed."""
        fake = FakeS3()
        exchange = mock.Mock()
        exchange.publish = mock.AsyncMock(side_effect=RuntimeError("channel closed"))
        with mock.patch.dict(os.environ, ENV, clear=False), _patched(fake), \
                mock.patch("app.services.serpwow.engine.rabbitmq_exchange", exchange):
            r = TestClient(app).post(
                "/uploads/relationship",
                files={"file": ("in.csv", GOOD_CSV, "text/csv")},
                data={"company_id": "c1", "company_name": "Acme"})

        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["upload_id"])
        # And the run really is in S3, i.e. the re-drive scan has something to find.
        with _patched(fake):
            self.assertIsNotNone(store.read_run_pointer(r.json()["upload_id"]))

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

    def test_batch_mode_reads_On_while_a_run_is_still_in_progress(self) -> None:
        """The UI's Batch chip is `serpwow_summary.is_batch`. This pipeline has no per-row
        LLM path — phase 2 is ALWAYS the Gemini Batch verdict pass — so an absent key made
        run_detail.js read undefined and render "Batch: Off", which was just wrong."""
        fake = FakeS3()
        with _patched(fake):
            store.write_run_pointer("run1", "acme/relationship/run1", "Acme")
            store.put_object(store.status_key("acme/relationship/run1"), {
                "rows_total": 10, "rows_scraped": 10, "phase": "cleaning",
                "updated_at": "2026-08-04T00:00:00Z"})
            r = TestClient(app).get("/uploads/run1/status")
        self.assertTrue(r.json()["serpwow_summary"]["is_batch"])

    def test_batch_mode_still_reads_On_from_a_terminal_report(self) -> None:
        """Same value from the other source, so the chip cannot flip when a run finishes."""
        fake = FakeS3()
        with _patched(fake):
            store.write_run_pointer("run1", "acme/relationship/run1", "Acme")
            store.put_object(store.status_key("acme/relationship/run1"),
                             {"rows_total": 10, "phase": "completed",
                              "updated_at": "2026-08-04T00:00:00Z"})
            store.put_object("acme/relationship/run1/report.json",
                             {"summary": {"status": "completed", "is_batch": True,
                                          "total_rows": 10}})
            r = TestClient(app).get("/uploads/run1/status")
        self.assertTrue(r.json()["serpwow_summary"]["is_batch"])

    def test_terminal_status_comes_from_the_report_not_the_phase(self) -> None:
        """phase only ever reaches "completed"; write_outputs computes
        completed_with_errors and _notify_terminal sends THAT to Supabase, so the
        detail page must agree with the Runs list."""
        fake = FakeS3()
        prefix = "acme/relationship/run3"
        with _patched(fake):
            store.write_run_pointer("run3", prefix, "Acme")
            store.put_object(store.status_key(prefix),
                             {"rows_total": 5, "rows_scraped": 4, "rows_failed": 1,
                              "phase": "completed"})
            store.put_bytes(f"{prefix}/report.json", b'{"summary": {'
                            b'"status": "completed_with_errors", "total_rows": 5,'
                            b'"outcome_breakdown": {"found": 3, "not_found": 1,'
                            b'"errored": 1}}}')
            r = TestClient(app).get("/uploads/run3/status")
        self.assertEqual(r.json()["status"], "completed_with_errors")

    def test_a_stopped_run_reads_as_terminal_for_the_poller(self) -> None:
        """run_detail.js has no rule for "stopped": passing it through would poll
        forever and never show the Files card."""
        fake = FakeS3()
        prefix = "acme/relationship/run4"
        with _patched(fake):
            store.write_run_pointer("run4", prefix, "Acme")
            store.put_object(store.status_key(prefix),
                             {"rows_total": 5, "phase": "stopped"})
            store.put_bytes(f"{prefix}/report.json",
                            b'{"summary": {"status": "stopped", "total_rows": 5}}')
            r = TestClient(app).get("/uploads/run4/status")
        self.assertEqual(r.json()["status"], "completed")

    def test_available_files_lists_only_files_that_exist(self) -> None:
        """A run that failed mid-scrape is terminal, so the UI renders the Files card —
        it must not offer four enabled links to objects that were never written."""
        fake = FakeS3()
        prefix = "acme/relationship/run5"
        with _patched(fake):
            store.write_run_pointer("run5", prefix, "Acme")
            store.put_object(store.status_key(prefix),
                             {"rows_total": 5, "rows_failed": 5, "phase": "failed"})
            failed = TestClient(app).get("/uploads/run5/status").json()

            store.put_bytes(f"{prefix}/confirmed_relation.csv", b"a\n")
            store.put_bytes(f"{prefix}/run.log", b"x\n")
            partial = TestClient(app).get("/uploads/run5/status").json()

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["serpwow_summary"]["available_files"], [])
        self.assertEqual(partial["serpwow_summary"]["available_files"],
                         ["confirmed_relation.csv", "run.log"])

    def test_a_running_run_does_not_pay_for_the_file_probe(self) -> None:
        fake = FakeS3()
        prefix = "acme/relationship/run6"
        with _patched(fake):
            store.write_run_pointer("run6", prefix, "Acme")
            store.put_object(store.status_key(prefix),
                             {"rows_total": 5, "phase": "scraping"})
            # Count the LISTs: an empty available_files proves nothing on its own when
            # the fixture has no output files to find.
            with mock.patch.object(fake, "get_paginator",
                                   wraps=fake.get_paginator) as paginator:
                body = TestClient(app).get("/uploads/run6/status").json()
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["serpwow_summary"]["available_files"], [])
        self.assertEqual(paginator.call_count, 0)

    def test_a_terminal_run_pays_one_scoped_list_per_file(self) -> None:
        """The counterpart: the probe really is one LIST per name, never a LIST of the
        run prefix (which holds ~1M raw/ and cleaned/ objects at 500k rows)."""
        fake = FakeS3()
        prefix = "acme/relationship/run6b"
        with _patched(fake):
            store.write_run_pointer("run6b", prefix, "Acme")
            store.put_object(store.status_key(prefix),
                             {"rows_total": 5, "phase": "completed"})
            with mock.patch.object(fake, "get_paginator",
                                   wraps=fake.get_paginator) as paginator:
                TestClient(app).get("/uploads/run6b/status")
        self.assertEqual(paginator.call_count, len(engine._RELATIONSHIP_FILES))

    def test_a_re_driven_run_reports_the_live_phase_not_the_stale_report(self) -> None:
        """report.json OUTLIVES its run: retry_failed_rows deletes the error markers,
        not the outputs. Trusting it while the re-drive is back at phase "scraping"
        would report the PREVIOUS run's terminal status for the whole re-run — the page
        would stop polling, hide progress, offer no Stop, and advertise stale files."""
        fake = FakeS3()
        prefix = "acme/relationship/run8"
        with _patched(fake):
            store.write_run_pointer("run8", prefix, "Acme")
            store.put_object(store.status_key(prefix),
                             {"rows_total": 5, "rows_scraped": 1, "phase": "scraping"})
            store.put_bytes(f"{prefix}/report.json",
                            b'{"summary": {"status": "completed_with_errors",'
                            b'"total_rows": 5}}')
            # The previous run's outputs are still sitting there too.
            store.put_bytes(f"{prefix}/confirmed_relation.csv", b"a\n")
            store.put_bytes(f"{prefix}/run.log", b"x\n")
            body = TestClient(app).get("/uploads/run8/status").json()

        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["phase"], "scraping")
        self.assertEqual(body["serpwow_summary"]["available_files"], [])


class FailureAnalysisTests(unittest.TestCase):
    """GET /uploads/{id}/failure-analysis — the endpoint behind the "View failed rows"
    control run_detail.js offers whenever outcome_breakdown.errored > 0."""

    PREFIX = "acme/relationship/run7"

    def test_failed_rows_come_from_the_error_objects(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            store.write_run_pointer("run7", self.PREFIX, "Acme")
            store.put_object(store.status_key(self.PREFIX), {"rows_total": 4})
            for idx, category in ((2, "timeout"), (1, "http_502")):
                store.put_object(store.error_key(self.PREFIX, idx), {
                    "row_index": idx, "error": f"boom {idx}",
                    "error_category": category,
                    "fields": {"y_name": f"Y{idx}"}})
            store.write_row(self.PREFIX, 3, {"row_index": 3})
            r = TestClient(app).get("/uploads/run7/failure-analysis",
                                    params={"sample_limit": 100})

        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["failed_rows"], 2)          # the raw object is not counted
        self.assertEqual(body["total_rows"], 4)
        self.assertEqual(body["failed_rate_pct"], 50.0)
        # Sorted by row_index, and shaped exactly as the JS table reads it.
        self.assertEqual([row["row_index"] for row in body["sample_failed_rows"]], [1, 2])
        first = body["sample_failed_rows"][0]
        self.assertEqual(first["company_name"], "Y1")
        self.assertEqual(first["error_source"], "scrapedo")
        self.assertEqual(first["error_category"], "http_502")
        self.assertEqual(first["error"], "boom 1")
        self.assertEqual(body["by_category"],
                         [{"category": "http_502", "count": 1},
                          {"category": "timeout", "count": 1}])

    def test_sample_limit_caps_the_gets_but_not_the_count(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            store.write_run_pointer("run7", self.PREFIX, "Acme")
            for idx in range(5):
                store.put_object(store.error_key(self.PREFIX, idx),
                                 {"row_index": idx, "error": "boom"})
            r = TestClient(app).get("/uploads/run7/failure-analysis",
                                    params={"sample_limit": 2})
        body = r.json()
        self.assertEqual(body["failed_rows"], 5)
        self.assertEqual(len(body["sample_failed_rows"]), 2)

    def test_the_sample_is_the_first_n_rows_not_a_lexicographic_slice(self) -> None:
        """Shard directories are numeric but unpadded, so raw/10/ sorts before raw/2/.
        Plain sorted() would sample rows 0, 10000, 10001... instead of the first two."""
        fake = FakeS3()
        with _patched(fake):
            store.write_run_pointer("run9", self.PREFIX, "Acme")
            for idx in (0, 2000, 10000, 10001):   # shards 0, 2, 10, 10
                store.put_object(store.error_key(self.PREFIX, idx),
                                 {"row_index": idx, "error": "boom"})
            r = TestClient(app).get("/uploads/run9/failure-analysis",
                                    params={"sample_limit": 2})
        body = r.json()
        self.assertEqual(body["failed_rows"], 4)
        self.assertEqual([row["row_index"] for row in body["sample_failed_rows"]],
                         [0, 2000])

    def test_a_clean_run_reports_zero_rather_than_404ing(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            store.write_run_pointer("run7", self.PREFIX, "Acme")
            r = TestClient(app).get("/uploads/run7/failure-analysis")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["failed_rows"], 0)
        self.assertEqual(r.json()["sample_failed_rows"], [])

    def test_a_non_relationship_id_still_404s(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            r = TestClient(app).get("/uploads/nope/failure-analysis")
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
            store.write_row(self.PREFIX, 4, {"ok": True})
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

    def test_rerun_failed_still_succeeds_when_publishing_fails(self) -> None:
        """The second unguarded publish call site: the error markers are already deleted
        by this point, so a 500 here leaves the user thinking nothing happened while the
        re-drive scan proceeds to rescrape those rows."""
        fake = self._seeded()
        exchange = mock.Mock()
        exchange.publish = mock.AsyncMock(side_effect=RuntimeError("channel closed"))
        with _patched(fake):
            store.put_object(store.error_key(self.PREFIX, 3), {"error": "boom"})
        with _patched(fake), mock.patch(
                "app.services.serpwow.engine.rabbitmq_exchange", exchange):
            r = TestClient(app).post("/uploads/run2/retry-failed-rows")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["retried_rows"], 1)


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
        self.assertNotIn("city", body["columns_detected"])
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
