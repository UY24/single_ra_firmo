"""Offline tests for SerpWow Slack notifications (terminal-state ping + dedup)."""
import asyncio
import unittest
from unittest import mock

from app.services.serpwow import legacy_app as la


class NotifyTerminalRoutingTests(unittest.TestCase):
    """_notify_slack_terminal maps upload state -> the right notify call."""

    def test_completed_with_errors_routes_to_complete(self):
        state = {"upload_id": "UP1", "company_name": "Acme Inc", "pipeline": "gmaps",
                 "status": "completed_with_errors", "total_rows": 100,
                 "success_rows": 90, "failed_rows": 10}
        with mock.patch("app.core.notify.notify_run_complete") as done, \
                mock.patch("app.core.notify.notify_run_failed") as failed:
            la._notify_slack_terminal(state)
        failed.assert_not_called()
        kw = done.call_args.kwargs
        self.assertEqual(kw["pipeline"], "Google Maps")     # raw key -> UI label
        self.assertEqual(kw["company"], "Acme Inc")
        self.assertEqual(kw["run_ref"], "UP1")
        self.assertEqual(kw["status"], "completed_with_errors")
        self.assertEqual(kw["success"], 90)
        self.assertEqual(kw["failed"], 10)
        self.assertEqual(kw["total_rows"], 100)

    def test_failed_routes_to_failed(self):
        state = {"upload_id": "UP2", "company_name": "Acme Inc", "pipeline": "full",
                 "status": "failed", "total_rows": 5, "success_rows": 0,
                 "failed_rows": 5, "error": "queue down"}
        with mock.patch("app.core.notify.notify_run_complete") as done, \
                mock.patch("app.core.notify.notify_run_failed") as failed:
            la._notify_slack_terminal(state)
        done.assert_not_called()
        kw = failed.call_args.kwargs
        self.assertEqual(kw["pipeline"], "Upload Console")
        self.assertEqual(kw["error"], "queue down")

    def test_gsearch_includes_searches_tokens_cost(self):
        state = {
            "upload_id": "UP3", "company_name": "ISI Market Test", "pipeline": "gsearch",
            "status": "completed", "total_rows": 1, "success_rows": 1, "failed_rows": 0,
            "processing_seconds_total": 12.0,
            "rows": [{"row_index": 0, "company_name": "ISI", "country": "us",
                      "status": "completed", "error": None, "result": {
                          "official_website": "https://isi.com", "gemini_cost_usd": 0.0002,
                          "context": {"cost_breakdown": {"serpwow_request_count": 3},
                                      "final_url_selection_ai": {"usage": {
                                          "promptTokenCount": 40, "candidatesTokenCount": 8},
                                          "raw": {"official_website": "https://isi.com",
                                                  "confidence_score": 80}}}}}],
        }
        with mock.patch("app.core.notify.notify_run_complete") as done, \
                mock.patch("app.core.notify.notify_run_failed"):
            la._notify_slack_terminal(state)
        kw = done.call_args.kwargs
        self.assertEqual(kw["search_label"], "SerpWow searches")
        self.assertEqual(kw["searches"], 3)
        self.assertEqual(kw["tokens"], 48)
        self.assertEqual(kw["input_tokens"], 40)
        self.assertEqual(kw["output_tokens"], 8)
        self.assertAlmostEqual(kw["cost_usd"], 0.0002)

    def test_non_gsearch_omits_search_token_cost(self):
        state = {"upload_id": "UP4", "company_name": "Acme Inc", "pipeline": "gmaps",
                 "status": "completed", "total_rows": 2, "success_rows": 2, "failed_rows": 0}
        with mock.patch("app.core.notify.notify_run_complete") as done, \
                mock.patch("app.core.notify.notify_run_failed"):
            la._notify_slack_terminal(state)
        kw = done.call_args.kwargs
        self.assertNotIn("searches", kw)
        self.assertNotIn("tokens", kw)
        self.assertNotIn("cost_usd", kw)

    def test_never_raises(self):
        with mock.patch("app.core.notify.notify_run_complete",
                        side_effect=RuntimeError("boom")):
            la._notify_slack_terminal({"upload_id": "x", "status": "completed",
                                       "pipeline": "gmaps"})  # must not raise


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
                mock.patch.object(la, "_finalize_gsearch_outputs", new=mock.AsyncMock()) as fin, \
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
        return {"upload_id": "UP1", "company_name": "Acme Inc", "pipeline": "gmaps",
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
