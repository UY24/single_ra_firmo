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

    def test_never_raises(self):
        with mock.patch("app.core.notify.notify_run_complete",
                        side_effect=RuntimeError("boom")):
            la._notify_slack_terminal({"upload_id": "x", "status": "completed",
                                       "pipeline": "gmaps"})  # must not raise


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
