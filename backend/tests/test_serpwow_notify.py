"""Offline tests for SerpWow Slack notifications (terminal-state ping + dedup)."""
import asyncio
import unittest
from unittest import mock

from app.services.serpwow import engine as la


class NotifyTerminalRoutingTests(unittest.TestCase):
    """_notify_slack_terminal maps upload state -> the right notify call."""

    def test_completed_with_errors_routes_to_complete(self):
        # gsearch is a REPORTING_PIPELINES member, so the ping carries the
        # found/not_found/errored trio (off outcome_breakdown) instead of the
        # old binary success/failed — and success/failed are no longer passed
        # at all for in-scope pipelines.
        state = {"upload_id": "UP1", "company_name": "Acme Inc", "pipeline": "gsearch",
                 "status": "completed_with_errors", "total_rows": 3,
                 "rows": [
                     {"company_name": "A", "country": "us", "status": "completed",
                      "error": None, "outcome": "found",
                      "result": {"official_website": "https://a.com", "context": {}}},
                     {"company_name": "B", "country": "us", "status": "completed",
                      "error": "no site", "outcome": "not_found",
                      "result": {"official_website": None, "context": {}}},
                     {"company_name": "C", "country": "us", "status": "failed",
                      "error": "SerpWow request timed out", "outcome": "error",
                      "error_source": "serpwow", "error_category": "timeout",
                      "result": {"official_website": None, "context": {}}},
                 ]}
        with mock.patch("app.core.notify.notify_run_complete") as done, \
                mock.patch("app.core.notify.notify_run_failed") as failed:
            la._notify_slack_terminal(state)
        failed.assert_not_called()
        kw = done.call_args.kwargs
        self.assertEqual(kw["pipeline"], "Google Search")   # raw key -> UI label
        self.assertEqual(kw["company"], "Acme Inc")
        self.assertEqual(kw["run_ref"], "UP1")
        self.assertEqual(kw["status"], "completed_with_errors")
        self.assertEqual(kw["found"], 1)
        self.assertEqual(kw["not_found"], 1)
        self.assertEqual(kw["errored"], 1)
        self.assertEqual(kw["error_sources"], {"serpwow": 1})
        self.assertNotIn("success", kw)
        self.assertNotIn("failed", kw)
        self.assertEqual(kw["total_rows"], 3)

    def test_failed_routes_to_failed(self):
        state = {"upload_id": "UP2", "company_name": "Acme Inc", "pipeline": "firmographics",
                 "status": "failed", "total_rows": 5, "success_rows": 0,
                 "failed_rows": 5, "error": "queue down"}
        with mock.patch("app.core.notify.notify_run_complete") as done, \
                mock.patch("app.core.notify.notify_run_failed") as failed:
            la._notify_slack_terminal(state)
        done.assert_not_called()
        kw = failed.call_args.kwargs
        self.assertEqual(kw["pipeline"], "Firmographics")
        self.assertEqual(kw["error"], "queue down")

    def test_gsearch_includes_searches_tokens_cost(self):
        state = {
            "upload_id": "UP3", "company_name": "ISI Market Test", "pipeline": "gsearch",
            "status": "completed", "total_rows": 1, "success_rows": 1, "failed_rows": 0,
            "processing_seconds_total": 12.0,
            "rows": [{"row_index": 0, "company_name": "ISI", "country": "us",
                      "status": "completed", "error": None, "outcome": "found", "result": {
                          "official_website": "https://isi.com", "gemini_cost_usd": 0.0002,
                          "context": {"cost_breakdown": {"serpwow_request_count": 3},
                                      "final_url_selection_ai": {"usage": {
                                          "promptTokenCount": 40, "candidatesTokenCount": 8},
                                          "raw": {"official_website": "https://isi.com",
                                                  "confidence_score": 80}}}}}],
        }
        with mock.patch("app.core.notify.notify_run_complete") as done, \
                mock.patch("app.core.notify.notify_run_failed"), \
                mock.patch.dict("os.environ", {"SERPWOW_USD_PER_SEARCH": "0.00035"}, clear=False):
            la._notify_slack_terminal(state)
        kw = done.call_args.kwargs
        self.assertEqual(kw["search_label"], "SerpWow searches")
        self.assertEqual(kw["searches"], 3)
        self.assertEqual(kw["tokens"], 48)
        self.assertEqual(kw["input_tokens"], 40)
        self.assertEqual(kw["output_tokens"], 8)
        # total_usd now includes both LLM (0.0002) + SerpWow (3 * 0.00035 = 0.00105)
        self.assertAlmostEqual(kw["cost_usd"], 0.00125, places=6)
        # LLM/SerpWow are also sent split, so Slack can show both + total.
        self.assertAlmostEqual(kw["llm_cost_usd"], 0.0002, places=6)
        self.assertAlmostEqual(kw["serpwow_cost_usd"], 0.00105, places=6)
        # gsearch is a REPORTING_PIPELINES member -> outcome trio, no old success/failed.
        self.assertEqual(kw["found"], 1)
        self.assertEqual(kw["not_found"], 0)
        self.assertEqual(kw["errored"], 0)
        self.assertNotIn("success", kw)
        self.assertNotIn("failed", kw)

    def test_firmographics_reports_scrapedo_credits(self):
        """firmographics joined COST_SUMMARY_PIPELINES in 2026-08-19: it spends scrape.do
        credits per row, so the ping must say so instead of reporting a bare row count."""
        state = {"upload_id": "UP4", "company_name": "Acme Inc", "pipeline": "firmographics",
                 "status": "completed", "total_rows": 2, "success_rows": 2, "failed_rows": 0,
                 "rows": [{"status": "completed", "outcome": "found",
                           "result": {"official_website": "https://a.com",
                                      "gemini_cost_usd": 0.0002,
                                      "context": {"cost_breakdown": {
                                          "scrapedo_requests": 2,
                                          "scrapedo_successful_requests": 2,
                                          "scrapedo_credits": 15,
                                          "scrapedo_search_successful": 1,
                                          "scrapedo_ai_overview_successful": 1}}}}]}
        with mock.patch("app.core.notify.notify_run_complete") as done, \
                mock.patch("app.core.notify.notify_run_failed"):
            la._notify_slack_terminal(state)
        kw = done.call_args.kwargs
        self.assertEqual(kw["credits"], 15)
        self.assertEqual(kw["search_label"], "Scrape.do requests")
        # Credits are not dollars: the provider USD figure must be suppressed, and the
        # only USD reported is the LLM's.
        self.assertIsNone(kw["serpwow_cost_usd"])
        self.assertNotIn("success", kw)
        self.assertNotIn("failed", kw)

    def test_gsearch_includes_searches_tokens_cost(self):
        # gsearch has real serpwow_searches/tokens/cost (LLM batch/per-row
        # confidence mode) -> the Slack ping must surface them like gsearch does.
        state = {
            "upload_id": "UP5", "company_name": "Acme Inc", "pipeline": "gsearch",
            "status": "completed", "total_rows": 1, "success_rows": 1, "failed_rows": 0,
            "processing_seconds_total": 5.0,
            "rows": [{"row_index": 0, "company_name": "Acme", "country": "us",
                      "status": "completed", "error": None, "outcome": "found", "result": {
                          "official_website": "https://acme.com", "gemini_cost_usd": 0.0001,
                          "context": {"cost_breakdown": {"serpwow_request_count": 2},
                                      "gemini_batch_ai": {"usage": {
                                          "promptTokenCount": 30, "candidatesTokenCount": 6},
                                          "raw": {"official_website": "https://acme.com",
                                                  "confidence_score": 75}}}}}],
        }
        with mock.patch("app.core.notify.notify_run_complete") as done, \
                mock.patch("app.core.notify.notify_run_failed"), \
                mock.patch.dict("os.environ", {"SERPWOW_USD_PER_SEARCH": "0.00035"}, clear=False):
            la._notify_slack_terminal(state)
        kw = done.call_args.kwargs
        self.assertEqual(kw["search_label"], "SerpWow searches")
        self.assertEqual(kw["searches"], 2)
        self.assertEqual(kw["tokens"], 36)
        self.assertEqual(kw["input_tokens"], 30)
        self.assertEqual(kw["output_tokens"], 6)
        # total_usd = LLM (0.0001) + SerpWow (2 * 0.00035 = 0.0007)
        self.assertAlmostEqual(kw["cost_usd"], 0.0008, places=6)
        self.assertAlmostEqual(kw["llm_cost_usd"], 0.0001, places=6)
        self.assertAlmostEqual(kw["serpwow_cost_usd"], 0.0007, places=6)
        # gsearch is a REPORTING_PIPELINES member -> outcome trio, no old success/failed.
        self.assertEqual(kw["found"], 1)
        self.assertEqual(kw["not_found"], 0)
        self.assertEqual(kw["errored"], 0)
        self.assertNotIn("success", kw)
        self.assertNotIn("failed", kw)

    def test_never_raises(self):
        with mock.patch("app.core.notify.notify_run_complete",
                        side_effect=RuntimeError("boom")):
            la._notify_slack_terminal({"upload_id": "x", "status": "completed",
                                       "pipeline": "gsearch"})  # must not raise


class NotifyRenderTests(unittest.TestCase):
    """notify_run_complete renders the SerpWow searches·cost cell and the split
    LLM/Total cost cell; AI-Mode-shaped calls (no split) render as before."""

    def test_signed_zero_cost_uses_standard_zero_format(self):
        from app.core import notify

        self.assertEqual(notify._fmt_usd(-0.0), "$0.00")

    def test_sub_micro_costs_use_explicit_thresholds(self):
        from app.core import notify

        self.assertEqual(notify._fmt_usd(0.000001), "$0.000001")
        self.assertEqual(notify._fmt_usd(-0.000001), "$-0.000001")
        self.assertEqual(notify._fmt_usd(0.0000001), "<$0.000001")
        self.assertEqual(notify._fmt_usd(-0.0000001), ">-$0.000001")

    def _fields(self, **kw):
        from app.core import notify
        captured = {}

        def fake_post(fallback, blocks):
            captured["blocks"] = blocks
            return True

        with mock.patch.object(notify, "_post", side_effect=fake_post):
            notify.notify_run_complete(**kw)
        # The two-column field grid is the one block carrying "fields".
        for b in captured["blocks"]:
            if b.get("type") == "section" and "fields" in b:
                return {f["text"].split("\n", 1)[0].strip("* "): f["text"]
                        for f in b["fields"]}
        return {}

    def test_serpwow_split_costs_rendered(self):
        fields = self._fields(
            pipeline="Google Search", company="ISI", run_ref="UP3",
            status="completed", found=1, not_found=0, errored=0,
            searches=3, search_label="SerpWow searches",
            cost_usd=0.00125, llm_cost_usd=0.0002, serpwow_cost_usd=0.00105,
        )
        # SerpWow cell: searches THEN cost, one field.
        serp = next(v for k, v in fields.items() if "SerpWow searches" in k)
        self.assertIn("3 searches", serp)
        self.assertIn("$0.00105", serp)
        # Cost cell: LLM + Total (SerpWow already shown above), not a bare total.
        cost = next(v for k, v in fields.items() if "Cost" in k)
        self.assertIn("LLM $0.0002", cost)
        self.assertIn("Total $0.00125", cost)

    def test_ai_mode_shaped_call_unchanged(self):
        # No split cost params (AI Mode / scrape.do flat fee) -> bare count + single cost.
        fields = self._fields(
            pipeline="AI Mode", company="ISI", run_ref="UP9",
            status="completed", found=2, not_found=1,
            searches=5, search_label="Scrape.do searches", cost_usd=1.23,
        )
        serp = next(v for k, v in fields.items() if "Scrape.do searches" in k)
        self.assertTrue(serp.strip().endswith("5"))  # bare count, no "· $"
        self.assertNotIn("searches ·", serp)
        cost = next(v for k, v in fields.items() if "Cost" in k)
        self.assertIn("$1.23", cost)
        self.assertNotIn("LLM $", cost)


class GsearchBatchDeferralTests(unittest.TestCase):
    """gsearch batch mode: terminal side-effects (Slack/Supabase/finalize) are
    deferred until the Gemini batch is terminal, then fire once."""

    def _state(self, batch_status):
        return {"upload_id": "UPB", "company_name": "ISI", "pipeline": "gsearch",
                "run_db_id": "db1", "gemini_batch": {"status": batch_status},
                "rows": [{"status": "completed", "row_index": 0, "company_name": "ISI",
                          "country": "us", "result": {"official_website": "https://isi.com",
                                                      "context": {}}}]}

    def _persist(self, state):
        with mock.patch.object(la, "write_upload_artifact", new=mock.AsyncMock()), \
                mock.patch.object(la, "maybe_start_gemini_batch_for_upload", new=mock.AsyncMock()), \
                mock.patch.object(la, "update_summary_cache"), \
                mock.patch.object(la, "build_upload_output_payload", return_value={}), \
                mock.patch.object(la, "_batch_postprocess_enabled_for", return_value=True), \
                mock.patch.object(la, "_finalize_serpwow_outputs", new=mock.AsyncMock()) as fin, \
                mock.patch.object(la, "_update_supabase_run", return_value=True) as sup, \
                mock.patch.object(la, "_notify_slack_terminal") as notify_term:
            asyncio.run(la.persist_upload_state("UPB", state))
            return notify_term, fin, sup

    def test_defers_while_batch_pending(self):
        for st in ("waiting_for_rows", "queued", "running"):
            notify_term, fin, sup = self._persist(self._state(st))
            notify_term.assert_not_called()
            fin.assert_not_called()
            sup.assert_not_called()

    def test_fires_once_after_batch_terminal(self):
        notify_term, fin, sup = self._persist(self._state("succeeded"))
        notify_term.assert_called_once()
        fin.assert_called_once()
        sup.assert_called_once()


class PersistDedupTests(unittest.TestCase):
    """The terminal Slack ping fires once per distinct snapshot, even across
    multiple persist_upload_state calls (which happen on every row update)."""

    def _state(self, statuses):
        return {"upload_id": "UP1", "company_name": "Acme Inc", "pipeline": "gsearch",
                "rows": [{"status": s} for s in statuses]}

    def _persist(self, state):
        # Stub the side-effecting collaborators so the test stays offline.
        with mock.patch.object(la, "write_upload_artifact",
                               new=mock.AsyncMock()), \
                mock.patch.object(la, "maybe_start_gemini_batch_for_upload",
                                  new=mock.AsyncMock()), \
                mock.patch.object(la, "update_summary_cache"), \
                mock.patch.object(la, "build_upload_output_payload",
                                  return_value={}), \
                mock.patch.object(la, "_notify_slack_terminal") as notify_term:
            asyncio.run(la.persist_upload_state("UP1", state))
            return notify_term

    def test_fires_once_for_same_terminal_snapshot(self):
        state = self._state(["completed", "completed"])  # terminal: completed:2:0
        n1 = self._persist(state)
        n1.assert_called_once()
        # Re-persist the SAME state object (marker already set) -> no second ping.
        n2 = self._persist(state)
        n2.assert_not_called()

    def test_fires_again_on_a_new_terminal_snapshot(self):
        state = self._state(["completed", "completed"])
        self._persist(state).assert_called_once()
        # A retry re-opens the upload and re-completes with different counters.
        state["rows"] = [{"status": "completed"}, {"status": "failed"}]
        self._persist(state).assert_called_once()  # new marker -> fires again

    def test_does_not_fire_while_still_processing(self):
        state = self._state(["completed", "queued"])  # processing, not terminal
        self._persist(state).assert_not_called()


if __name__ == "__main__":
    unittest.main()
