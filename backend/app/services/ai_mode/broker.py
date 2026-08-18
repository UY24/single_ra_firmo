# backend/app/services/ai_mode/broker.py
"""AI Mode's RabbitMQ layer: own channel + durable queue on the shared connection.

The SerpWow engine owns the connection (``engine.init_rabbitmq``) and passes it
to ``init_ai_mode_broker``. AI Mode gets its OWN channel because aio_pika QoS is
per-channel — scrape.do concurrency (default 20, up to ~200) must not share
SerpWow's prefetch (default 4). The queue is bound to the same direct exchange
under its own routing key, so the management UI shows both systems side by side.

Import direction: engine -> ai_mode.broker (never the reverse).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

import aio_pika

ai_mode_channel: Optional[aio_pika.abc.AbstractChannel] = None
ai_mode_queue: Optional[aio_pika.abc.AbstractQueue] = None
_exchange: Optional[aio_pika.abc.AbstractExchange] = None
last_error: Optional[str] = None


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except (ValueError, TypeError):
        return default


def queue_name() -> str:
    return os.getenv("AI_MODE_QUEUE", "ai_mode_jobs")


def routing_key() -> str:
    return os.getenv("AI_MODE_ROUTING_KEY", "ai_mode.scrape")


def worker_concurrency() -> int:
    """In-flight scrape messages for AI Mode's queue.

    Falls back to the shared ``WORKER_CONCURRENCY`` so a deployment that wants one
    number only sets that one; ``AI_MODE_WORKER_CONCURRENCY`` exists for when AI Mode
    needs to differ (its messages are batches lasting ~50s, vs a gmaps row at ~3.5s).
    Neither of these is a provider cap — concurrent scrape.do calls are bounded
    centrally by ``SCRAPEDO_CONCURRENCY`` (common.provider_limits), so raising these
    cannot breach the account limit.
    """
    return max(1, _int_env("AI_MODE_WORKER_CONCURRENCY",
                           _int_env("WORKER_CONCURRENCY", 20)))


async def init_ai_mode_broker(connection) -> None:
    """Create AI Mode's channel/exchange/queue on the engine's connection."""
    global ai_mode_channel, ai_mode_queue, _exchange, last_error
    try:
        ai_mode_channel = await connection.channel()
        await ai_mode_channel.set_qos(prefetch_count=worker_concurrency())
        exchange_name = os.getenv("RABBITMQ_EXCHANGE", "singleRA_search")
        _exchange = await ai_mode_channel.declare_exchange(
            exchange_name,
            aio_pika.ExchangeType.DIRECT,
            durable=True,
        )
        ai_mode_queue = await ai_mode_channel.declare_queue(queue_name(), durable=True)
        await ai_mode_queue.bind(_exchange, routing_key=routing_key())
        last_error = None
    except Exception as exc:
        ai_mode_channel = None
        ai_mode_queue = None
        _exchange = None
        last_error = str(exc)
        raise


def is_ready() -> bool:
    return _exchange is not None


async def publish_scrape_job(payload: dict[str, Any]) -> None:
    """Publish one persistent AI-Mode job (scrape or check) message."""
    if _exchange is None:
        raise RuntimeError(f"AI Mode broker is not initialized ({last_error or 'no connection'})")
    message = aio_pika.Message(
        body=json.dumps(payload).encode("utf-8"),
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        content_type="application/json",
    )
    # Bounded publish: never hang indefinitely under broker flow control; the
    # reconciler republishes anything that got lost.
    await asyncio.wait_for(
        _exchange.publish(message, routing_key=routing_key()), timeout=5.0
    )


async def publish_check(run_id: str) -> None:
    """Publish a completion-check kick for a run (no entities; worker recounts)."""
    await publish_scrape_job({"type": "check", "run_id": run_id})


async def get_queue_depth() -> Optional[int]:
    """Best-effort READY-message count of the AI Mode queue (None if unknown).

    aio_pika's Queue.declare() takes no ``passive`` kwarg — the probe must go
    through channel.declare_queue(..., passive=True), whose declaration_result
    carries message_count. Note this counts ready messages only; unacked
    in-flight deliveries are invisible, which is why reconciler staleness
    checks look at file activity too.
    """
    if ai_mode_channel is None:
        return None
    try:
        probe = await ai_mode_channel.declare_queue(queue_name(), passive=True)
        result = getattr(probe, "declaration_result", None)
        count = getattr(result, "message_count", None)
        if count is None:
            return None
        return max(0, int(count))
    except Exception:
        return None


async def close_ai_mode_broker() -> None:
    """Close AI Mode's channel and reset state (connection is the engine's)."""
    global ai_mode_channel, ai_mode_queue, _exchange
    channel = ai_mode_channel
    ai_mode_channel = None
    ai_mode_queue = None
    _exchange = None
    if channel is not None:
        try:
            await channel.close()
        except Exception:
            pass
