import asyncio
import signal

from app.services.serpwow import engine as app


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
