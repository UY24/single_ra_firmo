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
                with self.assertRaises(RuntimeError):
                    await worker._start_relationship_worker()
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


if __name__ == "__main__":
    unittest.main()
