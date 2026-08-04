import asyncio
import logging
import os
import signal

from app.services.serpwow import engine as app

_LOGGER = logging.getLogger(__name__)


async def _start_relationship_worker() -> None:
    """Own channel + queue for relationship runs, plus the stale-run re-drive scan.

    Best-effort: a relationship-side failure here must not take the SerpWow (or
    AI Mode) consumers down, mirroring how ai_mode's consumers are started.
    """
    from app.services.serpwow.relationship_runner import (
        consume_relationship_runs,
        redrive_stale_runs,
    )

    # Own channel: aio_pika QoS is per-channel, and a run message is held for hours.
    relationship_channel = await app.rabbitmq_connection.channel()
    await relationship_channel.set_qos(prefetch_count=1)
    await consume_relationship_runs(relationship_channel)

    async def _relationship_redrive_loop() -> None:
        while True:
            try:
                await redrive_stale_runs()
            except Exception:
                _LOGGER.exception("relationship re-drive scan failed")
            await asyncio.sleep(
                max(60, int(os.getenv("RELATIONSHIP_REDRIVE_SCAN_SEC", "300"))))

    asyncio.create_task(_relationship_redrive_loop())


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await app.startup_event()
    stop_wait_task: asyncio.Task | None = None
    try:
        await app.start_worker_consumers()
        print("Worker started. Consuming RabbitMQ jobs...")
        try:
            await _start_relationship_worker()
        except Exception as exc:
            print(f"[relationship-worker] consumer failed to start: {exc}")
        stop_wait_task = asyncio.create_task(stop_event.wait())
        consumer_tasks = list(app.rabbitmq_consumer_tasks)
        wait_targets: list[asyncio.Task] = [stop_wait_task, *consumer_tasks]
        done, _ = await asyncio.wait(wait_targets, return_when=asyncio.FIRST_COMPLETED)

        if stop_wait_task not in done:
            first_error: BaseException | None = None
            for task in consumer_tasks:
                if task in done and not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        first_error = exc
                        break
            if first_error is not None:
                print(f"Worker consumer crashed: {type(first_error).__name__}: {first_error}")
                raise RuntimeError("Worker consumer task crashed") from first_error
            print("A worker consumer exited unexpectedly; stopping worker process.")
    finally:
        if stop_wait_task is not None and not stop_wait_task.done():
            stop_wait_task.cancel()
            try:
                await stop_wait_task
            except asyncio.CancelledError:
                pass
        await app.shutdown_event()  # shield rarely helps here; just await directly


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
