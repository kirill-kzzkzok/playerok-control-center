import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.store import Store
from app.workers.notifications import Notifications
from app.workers.outbox import OutboxWorker
from app.workers.supervisor import Supervisor


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "supervisor.sqlite")
    yield value
    value.close()


async def test_restart_coalesces_incident_and_stops_cleanly(store, caplog):
    stop = asyncio.Event()
    attempts = 0

    async def worker():
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise ValueError("secret-cookie-never-log")
        stop.set()

    await Supervisor(store, base_delay=0.001, max_delay=0.002).run("sync", worker, stop)
    health = store.setting("runtime:worker:sync")
    assert attempts == 3
    assert health["state"] == "STOPPED"
    assert health["failures_total"] == 2
    assert health["consecutive_failures"] == 2
    assert len(store.rows("SELECT * FROM inbox WHERE kind='worker_failed'")) == 1
    assert "secret-cookie" not in caplog.text
    assert "secret-cookie" not in json.dumps(store.rows("SELECT * FROM settings"))
    assert "secret-cookie" not in json.dumps(store.rows("SELECT * FROM inbox"))


async def test_unexpected_normal_return_is_failure(store):
    stop = asyncio.Event()

    async def worker():
        if store.setting("runtime:worker:exit").get("failures_total"):
            stop.set()

    await Supervisor(store, base_delay=0.001, max_delay=0.001).run("exit", worker, stop)
    assert store.setting("runtime:worker:exit")["error_type"] == "WorkerExitedError"


async def test_persisted_backoff_survives_cancellation_and_new_supervisor(store):
    stop = asyncio.Event()
    deadline = time.time() + 3600
    store.set_setting("runtime:worker:sync", {"retry_at": deadline, "failures_total": 3})
    factory = AsyncMock()
    task = Supervisor(store).start("sync", factory, stop)
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.setting("runtime:worker:sync")["retry_at"] == deadline
    other = Supervisor(store).start("sync", factory, stop)
    await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(other, timeout=1)
    factory.assert_not_awaited()


async def test_running_cancellation_does_not_restart_or_alert(store):
    entered = asyncio.Event()

    async def worker():
        entered.set()
        await asyncio.Event().wait()

    task = Supervisor(store).start("sync", worker, asyncio.Event())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.setting("runtime:worker:sync")["state"] == "STOPPED"
    assert not store.rows("SELECT * FROM inbox")


async def test_database_failure_does_not_disable_restart(store, monkeypatch, caplog):
    stop = asyncio.Event()
    attempts = 0

    def broken(*args, **kwargs):
        raise OSError("private database path")

    monkeypatch.setattr(store, "setting", broken)
    monkeypatch.setattr(store, "transaction", broken)

    async def worker():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("secret")
        stop.set()

    await Supervisor(store, base_delay=0.001, max_delay=0.001).run("sync", worker, stop)
    assert attempts == 2
    assert "persistence failed: OSError" in caplog.text
    assert "private database path" not in caplog.text


async def test_worker_alert_bypasses_historical_baseline_and_broken_preferences(store):
    store.claim_first_owner(42)
    store.set_setting("notifications:42:preferences", "broken")
    stop = asyncio.Event()

    async def worker():
        stop.set()
        raise RuntimeError

    await Supervisor(store).run("sync", worker, stop)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)))
    notices = Notifications(store, bot)
    await notices.once()
    store.set_setting("sync:health", {"status": "OK"})
    await notices.once()
    bot.send_message.assert_awaited_once()
    assert "sync: RuntimeError" in bot.send_message.call_args.args[1]
    assert store.rows("SELECT status FROM notifications")[0]["status"] == "SUCCEEDED"


async def test_outbox_ack_then_storage_failure_restart_never_resends(store, monkeypatch):
    ident = store.enqueue("once", "reply", "chat", {"text": "test"})

    async def dispatched(action):
        store.mark_started(action)
        return {"message_id": "ack"}

    executor = AsyncMock(side_effect=dispatched)
    finish = store.finish
    calls = 0
    stop = asyncio.Event()

    def broken_finish(*args, **kwargs):
        raise OSError("lost disk after external ACK")

    monkeypatch.setattr(store, "finish", broken_finish)

    async def loop():
        nonlocal calls
        calls += 1
        worker = OutboxWorker(store, executor)
        if calls == 2:
            assert store.rows("SELECT status FROM outbox WHERE id=?", (ident,))[0]["status"] == "PROCESSING"
            store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (ident,))
            monkeypatch.setattr(store, "finish", finish)
        await worker.once()
        stop.set()

    await Supervisor(store, base_delay=0.001, max_delay=0.001).run("outbox", loop, stop)
    executor.assert_awaited_once()
    assert calls == 2
    row = store.rows("SELECT status,attempts,error_code FROM outbox WHERE id=?", (ident,))[0]
    assert row == {"status": "UNCERTAIN", "attempts": 1, "error_code": "LEASE_EXPIRED"}
