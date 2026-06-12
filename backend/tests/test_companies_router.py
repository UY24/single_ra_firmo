"""Task 14: /companies router + company-aware run lifecycle (all offline/mocked)."""
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.companies import router as companies_router
from app.services.ai_mode.ai_mode_service import _build_run_update


def make_app():
    app = FastAPI()
    app.include_router(companies_router)
    return app


class TestCompaniesRouter(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(make_app())

    def test_create_company_ok(self):
        svc = mock.MagicMock()
        svc.create_company.return_value = {"id": "u1", "name": "Acme"}
        with mock.patch("app.routers.companies.get_company_service", return_value=svc):
            res = self.client.post("/companies", json={"name": "Acme"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["id"], "u1")
        svc.create_company.assert_called_once_with("Acme")

    def test_create_company_duplicate_409(self):
        svc = mock.MagicMock()
        svc.create_company.side_effect = Exception(
            'duplicate key value violates unique constraint "companies_name_key"'
        )
        with mock.patch("app.routers.companies.get_company_service", return_value=svc):
            res = self.client.post("/companies", json={"name": "Acme"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("already exists", res.json()["detail"])

    def test_create_company_empty_name_400(self):
        svc = mock.MagicMock()
        with mock.patch("app.routers.companies.get_company_service", return_value=svc):
            res = self.client.post("/companies", json={"name": "   "})
        self.assertEqual(res.status_code, 400)
        svc.create_company.assert_not_called()

    def test_unconfigured_503(self):
        with mock.patch("app.routers.companies.get_company_service", return_value=None):
            for method, path in (
                ("post", "/companies"),
                ("get", "/companies"),
                ("get", "/companies/stats"),
                ("get", "/companies/runs"),
            ):
                kwargs = {"json": {"name": "Acme"}} if method == "post" else {}
                res = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(res.status_code, 503, path)
                self.assertIn("Supabase not configured", res.json()["detail"])

    def test_list_companies(self):
        svc = mock.MagicMock()
        svc.list_companies.return_value = [{"id": "u1"}, {"id": "u2"}]
        with mock.patch("app.routers.companies.get_company_service", return_value=svc):
            res = self.client.get("/companies")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["companies"]), 2)

    def test_company_stats(self):
        svc = mock.MagicMock()
        svc.company_stats.return_value = [{"id": "u1", "runs": 3}]
        with mock.patch("app.routers.companies.get_company_service", return_value=svc):
            res = self.client.get("/companies/stats")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["companies"][0]["runs"], 3)

    def test_list_runs_passes_filters(self):
        svc = mock.MagicMock()
        svc.list_runs.return_value = [{"id": "r1"}]
        with mock.patch("app.routers.companies.get_company_service", return_value=svc):
            res = self.client.get("/companies/runs?company_id=u1&pipeline=gmaps")
        self.assertEqual(res.status_code, 200)
        svc.list_runs.assert_called_once_with(company_id="u1", pipeline="gmaps")


class TestAiModeUploadCompanyValidation(unittest.TestCase):
    def setUp(self):
        from app.routers.ai_mode import router as ai_mode_router

        app = FastAPI()
        app.include_router(ai_mode_router)
        self.client = TestClient(app)
        self.csv = ("companies.csv", b"S.NO,Company Name,Country\n1,Acme,US\n", "text/csv")

    def test_unconfigured_503(self):
        with mock.patch("app.routers.ai_mode.get_company_service", return_value=None):
            res = self.client.post(
                "/uploads/ai-mode", files={"file": self.csv}, data={"company_id": "u1"}
            )
        self.assertEqual(res.status_code, 503)
        self.assertIn("Supabase not configured", res.json()["detail"])

    def test_get_company_raises_503(self):
        """Unreachable Supabase (network error) maps to 503, not a raw 500."""
        svc = mock.MagicMock()
        svc.get_company.side_effect = ConnectionError("name resolution failed")
        with mock.patch("app.routers.ai_mode.get_company_service", return_value=svc):
            res = self.client.post(
                "/uploads/ai-mode", files={"file": self.csv}, data={"company_id": "u1"}
            )
        self.assertEqual(res.status_code, 503)
        self.assertIn("Supabase unreachable", res.json()["detail"])

    def test_unknown_company_400(self):
        svc = mock.MagicMock()
        svc.get_company.return_value = None
        with mock.patch("app.routers.ai_mode.get_company_service", return_value=svc):
            res = self.client.post(
                "/uploads/ai-mode", files={"file": self.csv}, data={"company_id": "nope"}
            )
        self.assertEqual(res.status_code, 400)
        self.assertIn("unknown company_id", res.json()["detail"])

    def test_known_company_creates_run_and_persists_run_db_id(self):
        svc = mock.MagicMock()
        svc.get_company.return_value = {"id": "u1", "name": "Acme"}
        svc.create_run.return_value = "db-run-1"
        info = {"run_id": "r-abc", "total_rows": 1}
        with mock.patch("app.routers.ai_mode.get_company_service", return_value=svc), \
                mock.patch("app.services.ai_mode.ai_mode_service.prepare_ai_mode_run",
                           return_value=info) as prepare, \
                mock.patch("app.services.ai_mode.ai_mode_service.set_run_db_id") as set_id, \
                mock.patch("app.services.ai_mode.ai_mode_service.run_ai_mode_sync"):
            res = self.client.post(
                "/uploads/ai-mode", files={"file": self.csv}, data={"company_id": "u1"}
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["run_db_id"], "db-run-1")
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.kwargs["company_name"], "Acme")
        svc.create_run.assert_called_once_with(
            company_id="u1", pipeline="ai_bulk", run_ref="r-abc", total_rows=1
        )
        set_id.assert_called_once_with("r-abc", "db-run-1")

    def test_create_run_failure_does_not_break_upload(self):
        svc = mock.MagicMock()
        svc.get_company.return_value = {"id": "u1", "name": "Acme"}
        svc.create_run.return_value = None  # supabase down: never raises
        info = {"run_id": "r-abc", "total_rows": 1}
        with mock.patch("app.routers.ai_mode.get_company_service", return_value=svc), \
                mock.patch("app.services.ai_mode.ai_mode_service.prepare_ai_mode_run",
                           return_value=info), \
                mock.patch("app.services.ai_mode.ai_mode_service.set_run_db_id") as set_id, \
                mock.patch("app.services.ai_mode.ai_mode_service.run_ai_mode_sync"):
            res = self.client.post(
                "/uploads/ai-mode", files={"file": self.csv}, data={"company_id": "u1"}
            )
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("run_db_id", res.json())
        set_id.assert_not_called()


class TestBuildRunUpdate(unittest.TestCase):
    def test_maps_summary_to_run_fields(self):
        summary = {
            "status": "completed",
            "websites_found": 8,
            "websites_not_found": 2,
            "llm_errors": 1,
            "failed_request_count": 0,
            "token_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "batch_duration_seconds": 12.5,
            "completed_at": "2026-06-11T00:00:00+00:00",
            "cost": {"llm_usd": 0.1, "scrapedo_usd": 0.02, "total_usd": 0.12},
        }
        links = {"found.csv": "/runs/x/found.csv"}
        update = _build_run_update(summary, links)
        self.assertEqual(update["status"], "completed")
        self.assertEqual(update["success_count"], 8)
        self.assertEqual(update["failed_count"], 3)  # not_found + llm_errors
        self.assertEqual(update["websites_found"], 8)
        self.assertEqual(update["websites_not_found"], 2)
        self.assertEqual(update["token_usage"]["total_tokens"], 15)
        self.assertEqual(update["duration_seconds"], 12.5)
        self.assertEqual(update["file_links"], links)
        self.assertEqual(update["finished_at"], "2026-06-11T00:00:00+00:00")
        self.assertIsNone(update["error"])
        # cost (Task 15) flows into the runs row `cost jsonb` column
        self.assertEqual(update["cost"], {"llm_usd": 0.1, "scrapedo_usd": 0.02, "total_usd": 0.12})

    def test_completed_with_errors_sets_error(self):
        update = _build_run_update(
            {"status": "completed_with_errors", "failed_request_count": 2}, {}
        )
        self.assertEqual(update["status"], "completed_with_errors")
        self.assertIn("2 request(s) failed", update["error"])


class TestLegacyUpdateSupabaseRun(unittest.TestCase):
    def test_updates_run_from_state(self):
        from app.services.serpwow import legacy_app

        svc = mock.MagicMock()
        state = {
            "upload_id": "up-1",
            "run_db_id": "db-9",
            "status": "completed_with_errors",
            "success_rows": 7,
            "failed_rows": 3,
            "processing_seconds_total": 42.0,
        }
        with mock.patch("app.services.companies.get_company_service", return_value=svc):
            legacy_app._update_supabase_run(state)
        svc.update_run.assert_called_once()
        args, kwargs = svc.update_run.call_args
        self.assertEqual(args[0], "db-9")
        self.assertEqual(kwargs["status"], "completed_with_errors")
        self.assertEqual(kwargs["success_count"], 7)
        self.assertEqual(kwargs["failed_count"], 3)
        self.assertEqual(kwargs["duration_seconds"], 42.0)
        self.assertIn("state.json", kwargs["file_links"])
        self.assertIn("output.json", kwargs["file_links"])
        self.assertIn("finished_at", kwargs)

    def test_no_run_db_id_is_noop(self):
        from app.services.serpwow import legacy_app

        svc = mock.MagicMock()
        with mock.patch("app.services.companies.get_company_service", return_value=svc):
            legacy_app._update_supabase_run({"upload_id": "up-1", "status": "completed"})
        svc.update_run.assert_not_called()

    def test_never_raises(self):
        from app.services.serpwow import legacy_app

        with mock.patch(
            "app.services.companies.get_company_service",
            side_effect=RuntimeError("boom"),
        ):
            self.assertFalse(
                legacy_app._update_supabase_run({"upload_id": "u", "run_db_id": "x"})
            )

    def test_returns_update_run_result(self):
        from app.services.serpwow import legacy_app

        state = {"upload_id": "up-1", "run_db_id": "db-9", "status": "completed"}
        for update_ok in (True, False):
            svc = mock.MagicMock()
            svc.update_run.return_value = update_ok
            with mock.patch(
                "app.services.companies.get_company_service", return_value=svc
            ):
                self.assertEqual(legacy_app._update_supabase_run(state), update_ok)


class TestShouldSyncSupabase(unittest.TestCase):
    def test_unsynced_snapshot_syncs(self):
        from app.services.serpwow.legacy_app import _should_sync_supabase

        self.assertTrue(_should_sync_supabase({}, "completed:7:3"))

    def test_synced_marker_skips(self):
        from app.services.serpwow.legacy_app import _should_sync_supabase

        state = {"supabase_sync_marker": "completed:7:3"}
        self.assertFalse(_should_sync_supabase(state, "completed:7:3"))

    def test_failed_marker_skips_same_snapshot_but_retries_new_one(self):
        from app.services.serpwow.legacy_app import _should_sync_supabase

        state = {"supabase_sync_failed_marker": "completed_with_errors:5:5"}
        # Same snapshot whose sync already failed: don't re-stall every persist.
        self.assertFalse(_should_sync_supabase(state, "completed_with_errors:5:5"))
        # Different terminal snapshot (e.g. retry re-completed): retry once.
        self.assertTrue(_should_sync_supabase(state, "completed:10:0"))


if __name__ == "__main__":
    unittest.main()
