import asyncio
import logging
import threading

logger = logging.getLogger(__name__)


class ThreadCalls:
    """Keep serialization and ownership until synchronous work really finishes."""

    def __init__(self, lock):
        self.lock = lock
        self.pending = set()

    async def call(self, function, on_abandoned=None):
        cancelled = threading.Event()
        abandoned = False
        reported = False

        async def execute():
            async with self.lock:
                if cancelled.is_set():
                    return None

                def invoke():
                    # The executor queue may delay actual entry after the async lock.
                    if cancelled.is_set():
                        return None
                    return function(cancelled)

                return await asyncio.to_thread(invoke)

        def complete(task):
            nonlocal reported
            self.pending.discard(task)
            error = None if task.cancelled() else task.exception()
            if abandoned and not reported and on_abandoned:
                reported = True

                async def observe():
                    # Let the cancelled outer worker first journal PROCESSING -> UNCERTAIN.
                    await asyncio.sleep(0)
                    try:
                        on_abandoned(None if error or task.cancelled() else task.result(), error)
                    except Exception as failure:  # noqa: BLE001 -- observer must not expose raw transport data
                        logger.error("Abandoned call evidence failed: %s", type(failure).__name__)

                observation = asyncio.create_task(observe(), name="playerok-abandoned-evidence")
                self.pending.add(observation)
                observation.add_done_callback(self.pending.discard)

        task = asyncio.create_task(execute(), name="playerok-thread-call")
        self.pending.add(task)
        task.add_done_callback(complete)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            abandoned = True
            cancelled.set()
            if task.done():
                # Completion can win the race before the cancellation reaches this waiter.
                complete(task)
            raise

    async def drain(self):
        while self.pending:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self.pending)), return_exceptions=True
            )
            # gather() of already completed tasks may return without yielding on 3.12.
            # Let completion callbacks remove tasks and register evidence observers.
            await asyncio.sleep(0)


def calls_for(api):
    if not hasattr(api, "_thread_calls"):
        api._thread_calls = ThreadCalls(api.lock)
    return api._thread_calls


async def drain_calls(api):
    calls = getattr(api, "_thread_calls", None)
    if calls is not None:
        await calls.drain()
