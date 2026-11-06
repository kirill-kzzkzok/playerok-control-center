"""Queue fairness and backup polling, entirely local."""

import asyncio

from app.database.store import Store
from app.services.orders import Orders
from app.workers.sync import Synchronizer


def test_delivery_is_claimed_before_background_work_but_old_work_is_not_starved(tmp_path):
    store = Store(tmp_path / "priority.sqlite")
    try:
        background = store.enqueue("background", "reply", "chat", {"text": "test"})
        urgent = store.enqueue("urgent", "gift_send", "order", {})
        assert store.claim()["id"] == urgent
        store.execute("UPDATE outbox SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (background,))
        for index in range(10):
            store.enqueue(f"urgent-{index}", "gift_send", f"other-{index}", {})
        claimed = []
        for index in range(5):
            if index == 2:
                path = store.path
                store.close()
                store = Store(path)
            claimed.append(store.claim()["id"])
            if claimed[-1] == background:
                break
        # Four paid turns then an aged ordinary turn, despite a continuous
        # paid backlog and a restart; age no longer overtakes every new sale.
        assert background in claimed and len(claimed) <= 5
        assert len(claimed) == len(set(claimed))
    finally:
        store.close()


async def test_healthy_backup_poll_is_about_thirty_seconds_and_retains_backoff(tmp_path, monkeypatch):
    store = Store(tmp_path / "poll.sqlite")
    stop = asyncio.Event()
    sync = Synchronizer(store, None, Orders(store))
    waits = []

    async def once():
        sync.health = {"status": "OK"}

    async def pending():
        return 0

    async def wait(tasks, *, timeout, return_when):
        waits.append(timeout)
        stop.set()
        return set(), set(tasks)

    monkeypatch.setattr(sync, "once", once)
    monkeypatch.setattr(sync, "process_requests", pending)
    monkeypatch.setattr("app.workers.sync.random.uniform", lambda a, b: 0)
    monkeypatch.setattr("app.workers.sync.asyncio.wait", wait)
    try:
        await sync.run(stop)
        assert 29 <= waits[0] <= 30
    finally:
        store.close()
