# backend/tests/test_rerun.py
import csv as csv_module
import json, os, tempfile, unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.ai_mode.rerun import split_for_rerun

class TestSplitForRerun(unittest.TestCase):
    def test_split(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            (run_dir / "input.csv").write_text(
                "company_name,country\nAcme,Japan\nBeta,Germany\nGamma,France\n")
            report = {"entities": [
                {"sno": 1, "company_name": "Acme", "country": "Japan",
                 "website_url": "https://acme.jp", "confidence": 90,
                 "flags": [], "attempt_log": [], "error": None,
                 "company_local_name": None},
                {"sno": 2, "company_name": "Beta", "country": "Germany",
                 "website_url": None, "confidence": 0, "flags": [],
                 "attempt_log": [], "error": "llm timeout",
                 "company_local_name": None},
            ]}  # Gamma never made it into the report (never scraped)
            (run_dir / "final_report.json").write_text(json.dumps(report))
            retry_csv, carryover = split_for_rerun(run_dir)
            self.assertEqual(len(carryover), 1)            # Acme carried over
            self.assertIn("Beta", retry_csv)               # failed → retried
            self.assertIn("Gamma", retry_csv)              # unscraped → retried
            self.assertNotIn("Acme", retry_csv)

FAILED_STATUS = {
    "status": "failed", "mode": "ai_deep",
    "company_id": "u1", "company_name": "Acme", "run_db_id": "db-prev",
}

ACME_CARRY = {
    "sno": 1, "company_name": "Acme", "country": "Japan",
    "company_local_name": None, "website_url": "https://acme.jp",
    "confidence": 90, "flags": [], "attempt_log": [], "error": None,
}


class TestRerunEndpoint(unittest.TestCase):
    def setUp(self):
        from app.routers.ai_mode import router

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_unknown_run_404(self):
        with mock.patch("app.services.ai_mode.run_store.find_run_dir", return_value=None):
            res = self.client.post("/uploads/ai-mode/nope/rerun")
        self.assertEqual(res.status_code, 404)

    def test_ineligible_status_400(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("app.services.ai_mode.run_store.find_run_dir",
                           return_value=Path(d)), \
                mock.patch("app.services.ai_mode.ai_mode_service.get_ai_mode_status",
                           return_value={"status": "completed"}):
            res = self.client.post("/uploads/ai-mode/r1/rerun")
        self.assertEqual(res.status_code, 400)
        self.assertIn("completed", res.json()["detail"])

    def test_nothing_to_rerun_400(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("app.services.ai_mode.run_store.find_run_dir",
                           return_value=Path(d)), \
                mock.patch("app.services.ai_mode.ai_mode_service.get_ai_mode_status",
                           return_value=dict(FAILED_STATUS)), \
                mock.patch("app.services.ai_mode.rerun.split_for_rerun",
                           side_effect=ValueError(
                               "nothing to re-run: every row already succeeded")):
            res = self.client.post("/uploads/ai-mode/r1/rerun")
        self.assertEqual(res.status_code, 400)
        self.assertIn("nothing to re-run", res.json()["detail"])

    def test_unconfigured_supabase_503(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("app.services.ai_mode.run_store.find_run_dir",
                           return_value=Path(d)), \
                mock.patch("app.services.ai_mode.ai_mode_service.get_ai_mode_status",
                           return_value=dict(FAILED_STATUS)), \
                mock.patch("app.services.ai_mode.rerun.split_for_rerun",
                           return_value=("company_name,country\nBeta,Germany\n", [])), \
                mock.patch("app.routers.ai_mode.get_company_service", return_value=None):
            res = self.client.post("/uploads/ai-mode/r1/rerun")
        self.assertEqual(res.status_code, 503)
        self.assertIn("Supabase not configured", res.json()["detail"])

    def test_happy_path_with_company_name_fallback(self):
        """Supabase company lookup blips → fall back to the prev run's name."""
        with tempfile.TemporaryDirectory() as prev_d, tempfile.TemporaryDirectory() as new_d:
            prev_dir, new_dir = Path(prev_d), Path(new_d)
            dirs = {"prev-run": prev_dir, "new-run": new_dir}
            svc = mock.MagicMock()
            svc.get_company.side_effect = ConnectionError("supabase blip")
            svc.create_run.return_value = "db-new"
            info = {"run_id": "new-run", "total_rows": 1, "mode": "ai_deep"}
            with mock.patch("app.services.ai_mode.run_store.find_run_dir",
                            side_effect=lambda rid: dirs.get(rid)), \
                    mock.patch("app.services.ai_mode.ai_mode_service.get_ai_mode_status",
                               return_value=dict(FAILED_STATUS)), \
                    mock.patch("app.services.ai_mode.rerun.split_for_rerun",
                               return_value=("company_name,country\nBeta,Germany\n",
                                             [dict(ACME_CARRY)])), \
                    mock.patch("app.routers.ai_mode.get_company_service",
                               return_value=svc), \
                    mock.patch("app.services.ai_mode.ai_mode_service.prepare_ai_mode_run",
                               return_value=info) as prepare, \
                    mock.patch("app.services.ai_mode.ai_mode_service.set_status_fields") as set_fields, \
                    mock.patch("app.services.ai_mode.ai_mode_service.set_run_db_id") as set_id, \
                    mock.patch("app.services.ai_mode.ai_mode_service.run_ai_mode_sync") as run_sync:
                res = self.client.post("/uploads/ai-mode/prev-run/rerun")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["run_id"], "new-run")
            self.assertEqual(body["rerun_of_run_id"], "prev-run")
            self.assertEqual(body["carried_over"], 1)
            self.assertEqual(body["run_db_id"], "db-new")
            # prepare got the prev run's mode/company, fallback company name
            self.assertEqual(prepare.call_args.kwargs["mode_key"], "ai_deep")
            self.assertEqual(prepare.call_args.kwargs["company_name"], "Acme")
            self.assertEqual(prepare.call_args.kwargs["company_id"], "u1")
            self.assertEqual(prepare.call_args.args[1], "rerun_of_prev-run.csv")
            # carryover.json landed in the NEW run dir
            carried = json.loads((new_dir / "carryover.json").read_text(encoding="utf-8"))
            self.assertEqual(carried, [ACME_CARRY])
            # supabase run row links back to the prev run row
            svc.create_run.assert_called_once_with(
                company_id="u1", pipeline="ai_deep", run_ref="new-run",
                total_rows=1, rerun_of="db-prev",
            )
            set_id.assert_called_once_with("new-run", "db-new")
            set_fields.assert_called_once_with(
                "new-run", rerun_of_run_id="prev-run", carried_over=1)
            run_sync.assert_called_once_with("new-run")

    def test_happy_path_uses_supabase_company_name(self):
        with tempfile.TemporaryDirectory() as prev_d, tempfile.TemporaryDirectory() as new_d:
            dirs = {"prev-run": Path(prev_d), "new-run": Path(new_d)}
            svc = mock.MagicMock()
            svc.get_company.return_value = {"id": "u1", "name": "Acme Renamed"}
            svc.create_run.return_value = None  # tracking down: rerun still works
            info = {"run_id": "new-run", "total_rows": 2, "mode": "ai_deep"}
            with mock.patch("app.services.ai_mode.run_store.find_run_dir",
                            side_effect=lambda rid: dirs.get(rid)), \
                    mock.patch("app.services.ai_mode.ai_mode_service.get_ai_mode_status",
                               return_value=dict(FAILED_STATUS)), \
                    mock.patch("app.services.ai_mode.rerun.split_for_rerun",
                               return_value=("company_name,country\nBeta,Germany\n", [])), \
                    mock.patch("app.routers.ai_mode.get_company_service",
                               return_value=svc), \
                    mock.patch("app.services.ai_mode.ai_mode_service.prepare_ai_mode_run",
                               return_value=info) as prepare, \
                    mock.patch("app.services.ai_mode.ai_mode_service.set_status_fields"), \
                    mock.patch("app.services.ai_mode.ai_mode_service.set_run_db_id") as set_id, \
                    mock.patch("app.services.ai_mode.ai_mode_service.run_ai_mode_sync"):
                res = self.client.post("/uploads/ai-mode/prev-run/rerun")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(prepare.call_args.kwargs["company_name"], "Acme Renamed")
            self.assertNotIn("run_db_id", res.json())
            set_id.assert_not_called()
            # no successes carried over → no carryover.json
            self.assertFalse((dirs["new-run"] / "carryover.json").exists())


class FakeScrapeDoClient:
    def __init__(self, **kwargs):
        pass

    def search_google_ai_mode(self, query, extra_params=None):
        return {"text_blocks": [{"snippet": "notes"}], "references": []}, 1.0


class FakeLLMClient:
    def complete_json(self, messages):
        from app.services.ai_mode.models import TokenUsage
        arr = [{
            "sno": 1, "company_name": "Beta", "country": "Germany",
            "website_url": "https://beta.de", "confidence": 80,
            "flags": [], "attempt_log": [],
        }]
        return arr, TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)


class TestCarryoverMerge(unittest.TestCase):
    """run_ai_mode_sync merges carryover.json into found.csv + final_report."""

    FAKE_ENV = {
        "SCRAPEDO_TOKEN": "fake-token",
        "GEMINI_API_KEY": "fake-key",
        "AI_MODE_LLM_BATCH": "",
        "AI_MODE_LLM_PROVIDER": "gemini",
    }

    def setUp(self):
        from app.services.ai_mode import ai_mode_service, run_store

        self._tmp = tempfile.TemporaryDirectory()
        results_root = Path(self._tmp.name) / "ai_mode_results"
        self._patches = [
            mock.patch.dict(os.environ, self.FAKE_ENV),
            mock.patch.object(run_store, "AI_MODE_RESULTS_DIR", results_root),
            mock.patch.object(ai_mode_service, "ScrapeDoClient", FakeScrapeDoClient),
            mock.patch.object(ai_mode_service, "make_llm_client", lambda cfg: FakeLLMClient()),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_carryover_merged_into_outputs(self):
        from app.services.ai_mode import ai_mode_service, run_store

        info = ai_mode_service.prepare_ai_mode_run(
            b"company_name,country\nBeta,Germany\n", "rerun_of_prev.csv",
            mode_key="ai_bulk", company_name="Acme Corp", company_id="u1",
        )
        run_dir = run_store.find_run_dir(info["run_id"])
        (run_dir / "carryover.json").write_text(
            json.dumps([dict(ACME_CARRY)]), encoding="utf-8")

        ai_mode_service.run_ai_mode_sync(info["run_id"])

        with (run_dir / "found.csv").open(newline="", encoding="utf-8") as fh:
            rows = list(csv_module.DictReader(fh))
        self.assertEqual({r["company_name"] for r in rows}, {"Beta", "Acme"})
        acme_row = next(r for r in rows if r["company_name"] == "Acme")
        self.assertIn("carried_over: from previous run", acme_row["flags"])
        self.assertEqual(acme_row["website_url"], "https://acme.jp")

        report = json.loads((run_dir / "final_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["carried_over"], 1)
        # carried successes count in this run's websites_found...
        self.assertEqual(report["summary"]["websites_found"], 2)
        # ...but not in scrapedo/llm stats (one real request, no extra tokens)
        self.assertEqual(report["summary"]["scrapedo_request_count"], 1)
        self.assertEqual(report["summary"]["token_usage"]["total_tokens"], 15)
        acme_ent = next(e for e in report["entities"] if e["company_name"] == "Acme")
        self.assertIn({"flag": "carried_over", "why": "from previous run"},
                      acme_ent["flags"])

        status = ai_mode_service.get_ai_mode_status(info["run_id"])
        self.assertEqual(status["carried_over"], 1)
        self.assertEqual(status["websites_found"], 2)


if __name__ == "__main__":
    unittest.main()
