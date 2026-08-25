"""The PUBLISHER must declare the S3-only run queues, not just the worker.

Live failure this pins (run 7a03eaa0, 2026-08-25): the exchange is DIRECT, and the three
``*_runs`` queues were declared+bound only inside ``consume_runs`` — the worker. A run
published while the worker had never bound its queue against that broker was silently
DISCARDED by RabbitMQ: no error, no log, no message in any queue, and the run sat at
phase="queued" until the stale-run scan found it up to GMAPS_STALE_SEC (900s) later.
"""
import asyncio
import unittest

from app.services.serpwow import s3_run_driver as driver


class FakeQueue:
    def __init__(self, name: str) -> None:
        self.name = name
        self.bindings: list[tuple[object, str]] = []

    async def bind(self, exchange, routing_key: str) -> None:
        self.bindings.append((exchange, routing_key))


class FakeChannel:
    def __init__(self, fail_on: str | None = None) -> None:
        self.queues: dict[str, FakeQueue] = {}
        self.fail_on = fail_on

    async def declare_queue(self, name: str, durable: bool = False) -> FakeQueue:
        if name == self.fail_on:
            raise RuntimeError("broker said no")
        assert durable, f"{name} must be durable — a run message outlives a broker restart"
        queue = self.queues.setdefault(name, FakeQueue(name))
        return queue


class DeclareRunQueuesTests(unittest.TestCase):
    def test_all_three_queues_are_declared_and_bound(self) -> None:
        channel, exchange = FakeChannel(), object()
        asyncio.run(driver.declare_run_queues(channel, exchange))
        expected = dict(driver.run_queues())
        self.assertEqual(set(channel.queues), set(expected))
        self.assertEqual({"gmaps_runs", "relationship_runs", "firmographics_runs"},
                         set(channel.queues))
        for name, queue in channel.queues.items():
            self.assertEqual(queue.bindings, [(exchange, expected[name])],
                             f"{name} not bound to the exchange on its routing key")

    def test_one_queue_failing_does_not_stop_the_others(self) -> None:
        """Best-effort per queue, and it must NEVER raise: this runs inside the API's
        startup, so a broker hiccup here would take the whole API down."""
        channel = FakeChannel(fail_on="gmaps_runs")
        asyncio.run(driver.declare_run_queues(channel, object()))
        self.assertNotIn("gmaps_runs", channel.queues)
        self.assertEqual({"relationship_runs", "firmographics_runs"}, set(channel.queues))

    def test_routing_keys_match_what_the_publishers_send(self) -> None:
        """The binding is useless if it does not match the key engine.publish_* uses —
        that mismatch is the same silent drop, just with the queue existing."""
        from app.services.serpwow import (
            firmographics_runner,
            gmaps_runner,
            relationship_runner,
        )
        self.assertEqual(dict(driver.run_queues()), {
            gmaps_runner.GMAPS_QUEUE: gmaps_runner.GMAPS_ROUTING_KEY,
            relationship_runner.RELATIONSHIP_QUEUE:
                relationship_runner.RELATIONSHIP_ROUTING_KEY,
            firmographics_runner.FIRMOGRAPHICS_QUEUE:
                firmographics_runner.FIRMOGRAPHICS_ROUTING_KEY,
        })


if __name__ == "__main__":
    unittest.main()
