import asyncio
import logging
import signal

from app.services.common.env import get_int_env
from app.services.serpwow import engine as app

_LOGGER = logging.getLogger(__name__)


async def _start_s3_run_worker(label: str, consume, redrive, scan_env: str) -> None:
    """Own channel + queue for one S3-only pipeline, plus its stale-run re-drive scan.

    Best-effort: a failure here must not take the SerpWow (or AI Mode) consumers down,
    mirroring how ai_mode's consumers are started. Both S3-only pipelines
    (relationship, gmaps) start through here — one message per run each, so each needs
    its own channel and its own recovery loop.
    """
    async def _redrive_loop() -> None:
        while True:
            try:
                await redrive()
            except Exception:
                _LOGGER.exception("%s re-drive scan failed", label)
            # get_int_env, not int(os.getenv(...)): this sits OUTSIDE the try above, so a
            # junk value here would raise and kill the whole worker process (this task is
            # registered for main()'s failure detection). get_int_env falls back instead.
            await asyncio.sleep(max(60, get_int_env(scan_env, 300)))

    # Started BEFORE the channel/consumer setup below, and on purpose: this loop is what
    # recovers a run published while the worker (or its RabbitMQ connection) was down or
    # being replaced, so it must not be skipped just because the connection/channel isn't
    # ready *this* time — that would defeat the exact case the scan exists for. Registered
    # in the shared consumer-task list so it's cancelled on shutdown and watched by
    # main()'s failure detection, same as every other consumer task.
    app.rabbitmq_consumer_tasks.append(asyncio.create_task(_redrive_loop()))

    # Own channel: aio_pika QoS is per-channel, and a run message is held for hours.
    channel = await app.rabbitmq_connection.channel()
    await channel.set_qos(prefetch_count=1)
    await consume(channel)


async def _start_run_workers() -> None:
    from app.services.serpwow import (
        firmographics_runner,
        gmaps_runner,
        relationship_runner,
    )

    for label, module, scan_env, consume in (
        ("relationship", relationship_runner, "RELATIONSHIP_REDRIVE_SCAN_SEC",
         relationship_runner.consume_relationship_runs),
        ("gmaps", gmaps_runner, "GMAPS_REDRIVE_SCAN_SEC",
         gmaps_runner.consume_gmaps_runs),
        ("firmographics", firmographics_runner, "FIRMOGRAPHICS_REDRIVE_SCAN_SEC",
         firmographics_runner.consume_firmographics_runs),
    ):
        # Per-pipeline try: one pipeline's broker problem must not stop the others from
        # consuming, and all are already best-effort against the SerpWow consumers.
        try:
            await _start_s3_run_worker(
                label, consume, module.redrive_stale_runs, scan_env)
        except Exception as exc:
            print(f"[{label}-worker] consumer failed to start: {exc}")


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
        await _start_run_workers()
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
