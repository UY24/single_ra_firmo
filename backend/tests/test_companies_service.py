import os
import time
import unittest
from unittest import mock

from app.services.companies import CompanyService


def make_table_mock(result_data):
    table = mock.MagicMock()
    for m in ("insert", "select", "update", "eq", "order", "limit"):
        getattr(table, m).return_value = table
    table.execute.return_value = mock.MagicMock(data=result_data)
    return table


class TestCompanyService(unittest.TestCase):
    def test_create_company(self):
        client = mock.MagicMock()
        client.table.return_value = make_table_mock([{"id": "u1", "name": "Acme"}])
        svc = CompanyService(client)
        self.assertEqual(svc.create_company("Acme")["id"], "u1")
        client.table.assert_called_with("companies")

    def test_create_company_strips_name(self):
        client = mock.MagicMock()
        table = make_table_mock([{"id": "u1", "name": "Acme"}])
        client.table.return_value = table
        CompanyService(client).create_company("  Acme  ")
        table.insert.assert_called_once_with({"name": "Acme"})

    def test_list_companies(self):
        client = mock.MagicMock()
        client.table.return_value = make_table_mock([{"id": "u1"}, {"id": "u2"}])
        self.assertEqual(len(CompanyService(client).list_companies()), 2)
        client.table.assert_called_with("companies")

    def test_get_company_found(self):
        client = mock.MagicMock()
        table = make_table_mock([{"id": "u1", "name": "Acme"}])
        client.table.return_value = table
        company = CompanyService(client).get_company("u1")
        self.assertEqual(company["name"], "Acme")
        table.eq.assert_called_once_with("id", "u1")

    def test_get_company_not_found(self):
        client = mock.MagicMock()
        client.table.return_value = make_table_mock([])
        self.assertIsNone(CompanyService(client).get_company("missing"))

    def test_create_run_returns_id(self):
        client = mock.MagicMock()
        table = make_table_mock([{"id": "r1"}])
        client.table.return_value = table
        run_id = CompanyService(client).create_run(
            company_id="u1", pipeline="gmaps", run_ref="run-123", total_rows=10)
        self.assertEqual(run_id, "r1")
        client.table.assert_called_with("runs")
        table.insert.assert_called_once_with({
            "company_id": "u1", "pipeline": "gmaps", "run_ref": "run-123",
            "status": "queued", "total_rows": 10, "rerun_of": None})

    def test_create_run_exception_returns_none(self):
        client = mock.MagicMock()
        client.table.side_effect = RuntimeError("down")
        with self.assertLogs("app.services.companies", level="ERROR"):
            run_id = CompanyService(client).create_run(
                company_id="u1", pipeline="gmaps", run_ref="run-123")
        self.assertIsNone(run_id)

    def test_update_run_retries_then_succeeds(self):
        client = mock.MagicMock()
        good = make_table_mock([{"id": "r1"}])
        bad = mock.MagicMock(); bad.update.side_effect = RuntimeError("down")
        client.table.side_effect = [bad, bad, good]
        svc = CompanyService(client, retry_sleep=0)
        self.assertTrue(svc.update_run("r1", status="completed"))

    def test_update_run_gives_up_quietly(self):
        client = mock.MagicMock()
        bad = mock.MagicMock(); bad.update.side_effect = RuntimeError("down")
        client.table.side_effect = [bad, bad, bad]
        svc = CompanyService(client, retry_sleep=0)
        self.assertFalse(svc.update_run("r1", status="completed"))  # logs, never raises

    def test_update_run_no_id_is_noop(self):
        client = mock.MagicMock()
        self.assertFalse(CompanyService(client).update_run(None, status="completed"))
        client.table.assert_not_called()

    def test_list_runs_filter_chaining(self):
        client = mock.MagicMock()
        table = make_table_mock([{"id": "r1"}])
        client.table.return_value = table
        runs = CompanyService(client).list_runs(company_id="u1", pipeline="gmaps", limit=5)
        self.assertEqual(runs, [{"id": "r1"}])
        client.table.assert_called_with("runs")
        table.eq.assert_has_calls([
            mock.call("company_id", "u1"), mock.call("pipeline", "gmaps")])
        table.order.assert_called_once_with("created_at", desc=True)
        table.limit.assert_called_once_with(5)

    def test_list_runs_no_filters(self):
        client = mock.MagicMock()
        table = make_table_mock([])
        client.table.return_value = table
        CompanyService(client).list_runs()
        table.eq.assert_not_called()
        table.limit.assert_called_once_with(200)

    def test_company_stats_aggregation(self):
        companies = [
            {"id": "c1", "name": "Acme", "created_at": "2026-01-01"},
            {"id": "c2", "name": "Empty Co", "created_at": "2026-01-02"},
        ]
        runs = [
            {"company_id": "c1", "status": "completed", "total_rows": 100,
             "success_count": 90, "failed_count": 10,
             "websites_found": 80, "websites_not_found": 20,
             "cost": {"total_usd": 1.25, "scrapedo_searches": 12},
             "token_usage": {"total_tokens": 1000, "prompt_tokens": 700,
                             "completion_tokens": 300}},
            {"company_id": "c1", "status": "failed", "total_rows": 50,
             "success_count": None, "failed_count": None,
             "websites_found": None, "websites_not_found": None,
             "cost": None, "token_usage": None},
        ]
        client = mock.MagicMock()
        client.table.side_effect = [make_table_mock(companies), make_table_mock(runs)]
        stats = CompanyService(client).company_stats()
        self.assertEqual(len(stats), 2)
        acme = next(s for s in stats if s["id"] == "c1")
        self.assertEqual(acme["runs"], 2)
        self.assertEqual(acme["total_rows"], 150)
        self.assertEqual(acme["success_count"], 90)
        self.assertEqual(acme["failed_count"], 10)
        self.assertEqual(acme["websites_found"], 80)
        self.assertEqual(acme["websites_not_found"], 20)
        self.assertEqual(acme["total_cost_usd"], 1.25)
        self.assertEqual(acme["total_tokens"], 1000)
        self.assertEqual(acme["total_input_tokens"], 700)
        self.assertEqual(acme["total_output_tokens"], 300)
        self.assertEqual(acme["total_searches"], 12)
        empty = next(s for s in stats if s["id"] == "c2")
        self.assertEqual(empty["runs"], 0)
        self.assertEqual(empty["total_rows"], 0)
        self.assertEqual(empty["total_cost_usd"], 0)
        self.assertEqual(empty["total_tokens"], 0)
        self.assertEqual(empty["total_input_tokens"], 0)
        self.assertEqual(empty["total_output_tokens"], 0)
        self.assertEqual(empty["total_searches"], 0)


class TestSupabaseClient(unittest.TestCase):
    def setUp(self):
        import app.core.supabase_client as sc
        sc._client = None
        sc._attempted = False
        sc._config_error = None
        self.sc = sc

    tearDown = setUp

    def test_returns_none_when_unconfigured(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(self.sc.get_supabase())  # must not raise
            # second call short-circuits via _attempted, still None
            self.assertIsNone(self.sc.get_supabase())

    def test_get_company_service_none_when_unconfigured(self):
        from app.services.companies import get_company_service
        env = {k: v for k, v in os.environ.items()
               if k not in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(get_company_service())

    def test_create_client_gets_short_timeouts(self):
        """Default postgrest timeout is 120s — we must pass 10s ClientOptions."""
        env = {"SUPABASE_URL": "https://example.supabase.co",
               "SUPABASE_SERVICE_ROLE_KEY": f"{'a' * 40}.{'b' * 80}.{'c' * 80}"}
        fake_client = mock.MagicMock()
        with mock.patch.dict(os.environ, env), \
                mock.patch("supabase.create_client",
                           return_value=fake_client) as create_client:
            self.assertIs(self.sc.get_supabase(), fake_client)
        create_client.assert_called_once()
        args, kwargs = create_client.call_args
        self.assertEqual(args, ("https://example.supabase.co", env["SUPABASE_SERVICE_ROLE_KEY"]))
        options = kwargs["options"]
        self.assertEqual(options.postgrest_client_timeout, 10)
        self.assertEqual(options.storage_client_timeout, 10)

    def test_postgres_connection_string_is_invalid(self):
        env = {"SUPABASE_URL": "https://example.supabase.co:5432/postgres",
               "SUPABASE_SERVICE_ROLE_KEY": f"{'a' * 40}.{'b' * 80}.{'c' * 80}"}
        with mock.patch.dict(os.environ, env), \
                mock.patch("supabase.create_client") as create_client:
            self.assertIsNone(self.sc.get_supabase())
        create_client.assert_not_called()
        self.assertIn("bare project REST URL", self.sc.get_supabase_config_error())

    def test_concurrent_init_never_returns_none(self):
        """Two parallel callers (FastAPI threadpool) must not see the half-set
        singleton while the first caller is still inside the slow create_client."""
        import threading
        env = {"SUPABASE_URL": "https://example.supabase.co",
               "SUPABASE_SERVICE_ROLE_KEY": f"{'a' * 40}.{'b' * 80}.{'c' * 80}"}
        fake_client = mock.MagicMock()
        both_started = threading.Barrier(2)

        def slow_create_client(*args, **kwargs):
            # Widen the init window so a second caller is guaranteed to race in.
            both_started.wait(timeout=5)
            time.sleep(0.1)
            return fake_client

        results = []

        def worker():
            results.append(self.sc.get_supabase())

        with mock.patch.dict(os.environ, env), \
                mock.patch("supabase.create_client", side_effect=slow_create_client) as cc:
            # The first thread enters create_client and waits on the barrier; the
            # second thread is released into get_supabase by the same barrier, so it
            # arrives while the singleton is still being built.
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=lambda: (both_started.wait(timeout=5), worker()))
            t1.start(); t2.start()
            t1.join(timeout=5); t2.join(timeout=5)

        self.assertEqual(results, [fake_client, fake_client])  # neither got None
        cc.assert_called_once()  # init ran exactly once, not per-thread


if __name__ == "__main__":
    unittest.main()
