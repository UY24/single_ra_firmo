# backend/tests/test_ai_mode_broker_publish.py
"""AI Mode broker layer (PR2): own channel/queue on the shared connection.

Offline: aio_pika connection/channel/exchange/queue are fakes capturing calls.
"""
import asyncio
import json
import os
import unittest
from unittest import mock

from app.services.ai_mode import broker


class FakeExchange:
    def __init__(self):
        self.published = []  # (body_dict, routing_key)

    async def publish(self, message, routing_key):
        self.published.append((json.loads(message.body.decode("utf-8")), routing_key))


class FakeDeclarationResult:
    def __init__(self, message_count):
        self.message_count = message_count


class FakeQueue:
    def __init__(self, message_count=7):
        self.bound = []
        self.declaration_result = FakeDeclarationResult(message_count)

    async def bind(self, exchange, routing_key):
        self.bound.append((exchange, routing_key))


class FakeChannel:
    def __init__(self):
        self.qos_prefetch = None
        self.declared_exchanges = []
        self.declared_queues = []
        self.exchange = FakeExchange()
        self.queue = FakeQueue()
        self.closed = False

    async def set_qos(self, prefetch_count):
        self.qos_prefetch = prefetch_count

    async def declare_exchange(self, name, type_, durable=False):
        self.declared_exchanges.append((name, durable))
        return self.exchange

    async def declare_queue(self, name, durable=False, passive=False):
        # Mirrors aio_pika: non-passive declares are recorded; a passive call is
        # the depth probe and returns the same queue (declaration_result).
        if not passive:
            self.declared_queues.append((name, durable))
        return self.queue

    async def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self._channel = FakeChannel()

    async def channel(self):
        return self._channel


def _run(coro):
    return asyncio.run(coro)


class TestAiModeBroker(unittest.TestCase):
    def setUp(self):
        _run(broker.close_ai_mode_broker())
        self.conn = FakeConnection()

    def tearDown(self):
        _run(broker.close_ai_mode_broker())

    def test_init_declares_own_channel_queue_and_qos(self):
        with mock.patch.dict(os.environ, {"AI_MODE_WORKER_CONCURRENCY": "20"}):
            _run(broker.init_ai_mode_broker(self.conn))
        ch = self.conn._channel
        self.assertEqual(ch.qos_prefetch, 20)
        self.assertEqual(ch.declared_queues, [("ai_mode_jobs", True)])
        self.assertEqual(ch.queue.bound[0][1], "ai_mode.scrape")
        self.assertTrue(broker.is_ready())

    def test_publish_scrape_job_round_trips_payload(self):
        _run(broker.init_ai_mode_broker(self.conn))
        payload = {"type": "scrape", "run_id": "r1", "request_index": 3,
                   "entities": [{"company_name": "Acme"}]}
        _run(broker.publish_scrape_job(payload))
        published = self.conn._channel.exchange.published
        self.assertEqual(len(published), 1)
        body, routing_key = published[0]
        self.assertEqual(body, payload)
        self.assertEqual(routing_key, "ai_mode.scrape")

    def test_publish_check_message(self):
        _run(broker.init_ai_mode_broker(self.conn))
        _run(broker.publish_check("r9"))
        body, _ = self.conn._channel.exchange.published[0]
        self.assertEqual(body["type"], "check")
        self.assertEqual(body["run_id"], "r9")

    def test_publish_raises_when_not_ready(self):
        self.assertFalse(broker.is_ready())
        with self.assertRaises(RuntimeError):
            _run(broker.publish_scrape_job({"type": "scrape"}))

    def test_queue_depth(self):
        self.assertIsNone(_run(broker.get_queue_depth()))
        _run(broker.init_ai_mode_broker(self.conn))
        self.assertEqual(_run(broker.get_queue_depth()), 7)

    def test_close_resets_state(self):
        _run(broker.init_ai_mode_broker(self.conn))
        _run(broker.close_ai_mode_broker())
        self.assertFalse(broker.is_ready())
        self.assertTrue(self.conn._channel.closed)


class TestUploadEndpointBrokerGate(unittest.TestCase):
    """POST /uploads/ai-mode 503s BEFORE creating anything when the queue is down."""

    def test_upload_503_when_broker_down(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routers.ai_mode import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        self.assertFalse(broker.is_ready())
        res = client.post(
            "/uploads/ai-mode",
            files={"file": ("input.csv", b"company_name,country\nAcme,US\n", "text/csv")},
            data={"mode": "ai_bulk", "company_id": "c1"},
        )
        self.assertEqual(res.status_code, 503)
        self.assertIn("queue", res.json()["detail"].lower())


if __name__ == "__main__":
    unittest.main()
