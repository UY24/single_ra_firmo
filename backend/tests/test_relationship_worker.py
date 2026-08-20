"""worker.py wiring for the relationship consumer + re-drive scan (M4/M5 from review).

The re-drive loop must survive a broker/channel setup failure (M4: it is exactly what
recovers a run published while the worker or its connection was down, so it must not be
silently skipped when the connection isn't ready) and must be registered where main()'s
consumer-task snapshot and shutdown's cancel loop both look (M5)."""
import asyncio
import unittest
from unittest import mock

from app.services.serpwow import engine as app_engine
from app.services.serpwow import worker


class RelationshipWorkerWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_tasks = list(app_engine.rabbitmq_consumer_tasks)
        app_engine.rabbitmq_consumer_tasks = []

    def tearDown(self) -> None:
        async def _drain():
            for task in app_engine.rabbitmq_consumer_tasks:
                task.cancel()
            for task in app_engine.rabbitmq_consumer_tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if app_engine.rabbitmq_consumer_tasks:
            asyncio.run(_drain())
        app_engine.rabbitmq_consumer_tasks = self._saved_tasks

    def test_redrive_loop_survives_a_channel_setup_failure_and_is_registered(self) -> None:
        class BoomConnection:
            async def channel(self):
                raise RuntimeError("connection is down")

        results: dict = {}

        async def run():
            with mock.patch.object(app_engine, "rabbitmq_connection", BoomConnection()), \
                    mock.patch(
                        "app.services.serpwow.relationship_runner.redrive_stale_runs",
                        new=mock.AsyncMock(return_value=0)):
                from app.services.serpwow import relationship_runner
                with self.assertRaises(RuntimeError):
                    await worker._start_s3_run_worker(
                        "relationship",
                        relationship_runner.consume_relationship_runs,
                        relationship_runner.redrive_stale_runs,
                        "RELATIONSHIP_REDRIVE_SCAN_SEC")
                await asyncio.sleep(0)   # let the created task get scheduled
                # Must be checked HERE, inside the same event loop: asyncio.run()
                # cancels every outstanding task as part of its own teardown once
                # `run()` returns, so checking .done() after asyncio.run(run())
                # would always see "cancelled/done" regardless of whether the
                # production code actually kept the task alive.
                results["tasks"] = list(app_engine.rabbitmq_consumer_tasks)
                results["done"] = results["tasks"][0].done() if results["tasks"] else None

        asyncio.run(run())

        # M5: registered in the SAME list main() snapshots for its wait_targets and
        # stop_worker_consumers cancels — not a separate, unwatched task.
        self.assertEqual(len(results["tasks"]), 1)
        # M4: still alive despite the channel setup having raised.
        self.assertFalse(results["done"])

    def test_every_s3_only_pipeline_gets_a_consumer_and_a_redrive_loop(self) -> None:
        """gmaps joined relationship on the run-per-message model in 2026-08, and
        firmographics in 2026-08-20. Missing any one of them leaves that pipeline's uploads
        sitting in a queue nobody reads, with no re-drive scan to rescue them either."""
        started: list[str] = []

        class FakeChannel:
            async def set_qos(self, prefetch_count): pass

        class FakeConnection:
            async def channel(self): return FakeChannel()

        async def run():
            with mock.patch.object(app_engine, "rabbitmq_connection", FakeConnection()), \
                    mock.patch(
                        "app.services.serpwow.relationship_runner.consume_relationship_runs",
                        new=mock.AsyncMock(side_effect=lambda ch: started.append("relationship"))), \
                    mock.patch(
                        "app.services.serpwow.gmaps_runner.consume_gmaps_runs",
                        new=mock.AsyncMock(side_effect=lambda ch: started.append("gmaps"))), \
                    mock.patch(
                        "app.services.serpwow.relationship_runner.redrive_stale_runs",
                        new=mock.AsyncMock(return_value=0)), \
                    mock.patch(
                        "app.services.serpwow.gmaps_runner.redrive_stale_runs",
                        new=mock.AsyncMock(return_value=0)), \
                    mock.patch(
                        "app.services.serpwow.firmographics_runner"
                        ".consume_firmographics_runs",
                        new=mock.AsyncMock(
                            side_effect=lambda ch: started.append("firmographics"))), \
                    mock.patch(
                        "app.services.serpwow.firmographics_runner.redrive_stale_runs",
                        new=mock.AsyncMock(return_value=0)):
                await worker._start_run_workers()
                return len(app_engine.rabbitmq_consumer_tasks)

        redrive_loops = asyncio.run(run())
        self.assertEqual(sorted(started), ["firmographics", "gmaps", "relationship"])
        self.assertEqual(redrive_loops, 3)


if __name__ == "__main__":
    unittest.main()
