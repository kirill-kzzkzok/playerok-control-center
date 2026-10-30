import asyncio
import logging
import random
import time
import uuid

from app.database.store import encode, now

logger = logging.getLogger(__name__)


class WorkerExitedError(RuntimeError):
    pass


class Supervisor:
    """Restart loops, never individual requests; durable journals decide operation recovery."""

    def __init__(self, store, base_delay=5, max_delay=300, stable_seconds=60):
        if not 0 < base_delay <= max_delay or stable_seconds <= 0:
            raise ValueError("Invalid supervisor delays")
        self.store = store
        self.base_delay, self.max_delay = base_delay, max_delay
        self.stable_seconds = stable_seconds

    def start(self, name, factory, stop):
        return asyncio.create_task(self.run(name, factory, stop), name=name)

    def save(self, name, health, alert=False):
        health["updated_at"] = now()
        try:
            with self.store.transaction() as db:
                db.execute(
                    "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("runtime:worker:" + name, encode(health)),
                )
                if alert:
                    timestamp = now()
                    db.execute(
                        "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) "
                        "VALUES(?,'worker_failed',?,?,?)",
                        (
                            f"worker_failed:{name}:{health['incident']}",
                            encode({"worker": name, "error_type": health["error_type"]}),
                            timestamp,
                            timestamp,
                        ),
                    )
            return True
        except Exception as error:  # noqa: BLE001 -- persistence failure must not kill supervision
            logger.error("Worker health persistence failed: %s", type(error).__name__)
            return False

    async def pause(self, stop, delay):
        if delay > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def run(self, name, factory, stop):
        try:
            health = self.store.setting("runtime:worker:" + name, {})
        except Exception as error:  # noqa: BLE001 -- continue with in-memory backoff if DB unavailable
            logger.error("Worker health read failed: %s", type(error).__name__)
            health = {}
        pending_alert = False
        try:
            while not stop.is_set():
                await self.pause(stop, health.get("retry_at", 0) - time.time())
                if stop.is_set():
                    break
                health["state"] = "RUNNING"
                health["retry_at"] = 0
                if self.save(name, health, alert=pending_alert):
                    pending_alert = False
                started = time.monotonic()
                try:
                    await factory()
                    if stop.is_set():
                        break
                    raise WorkerExitedError
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 -- sanitized restart boundary, no request replay
                    timestamp = time.time()
                    if time.monotonic() - started >= self.stable_seconds:
                        health["consecutive_failures"] = 0
                    failures = health.get("consecutive_failures", 0) + 1
                    if timestamp - health.get("last_failure", 0) > 600 or not health.get("incident"):
                        health["incident"] = uuid.uuid4().hex
                    delay = min(self.max_delay, self.base_delay * 2 ** min(failures - 1, 20))
                    delay = min(self.max_delay, delay * random.uniform(1, 1.2))
                    health.update(
                        state="BACKOFF",
                        consecutive_failures=failures,
                        failures_total=health.get("failures_total", 0) + 1,
                        last_failure=timestamp,
                        retry_at=timestamp + delay,
                        error_type=type(error).__name__,
                    )
                    logger.error("Worker %s stopped: %s; restart delayed", name, type(error).__name__)
                    pending_alert = not self.save(name, health, alert=True)
        finally:
            health["state"] = "STOPPED"
            self.save(name, health, alert=pending_alert)
