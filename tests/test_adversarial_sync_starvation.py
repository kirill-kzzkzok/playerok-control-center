import asyncio
from types import SimpleNamespace

from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.sync import Synchronizer
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


async def test_slow_history_does_not_hold_deal_hint_or_paid_delivery(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "no-starvation")
    sync = Synchronizer(r.store, r.api, Orders(r.store))
    sync.ingest_chat({"id": "old"})
    entered, release = asyncio.Event(), asyncio.Event()
    original = r.api.call

    async def read(method, **kwargs):
        if method == "get_chat_messages" and kwargs["chat_id"] == "old":
            entered.set()
            await release.wait()
        return await original(method, **kwargs)

    r.api.call = read
    scan = asyncio.create_task(sync.once())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        r.api.order("paid")
        sync.request("deal", "paid")
        await asyncio.wait_for(sync.process_requests(), 0.5)
        assert not scan.done()
        await EnrollmentWorker(r.store, r.api).once()
        for _ in range(4):
            ChainWorker(r.store, r.api).once()
            await r.worker.once()
        assert sum(m["text"] == "no-starvation" for m in r.api.messages) == 1
    finally:
        release.set()
        await scan


async def test_production_polling_continues_while_history_is_still_waiting(shop, monkeypatch):
    r = await shop()
    sync = Synchronizer(r.store, r.api, Orders(r.store))
    sync.ingest_chat({"id": "old"})
    entered, release, seen, stop = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    clock = [100.0]
    calls = 0
    original = r.api.call

    async def read(method, **kwargs):
        nonlocal calls
        if method == "get_deals":
            calls += 1
            if calls == 2:
                assert entered.is_set() and not release.is_set()
                r.api.order("offline-paid")
                seen.set()
                stop.set()
        if method == "get_chat_messages" and kwargs["chat_id"] == "old":
            entered.set()
            await release.wait()
        return await original(method, **kwargs)

    r.api.call = read

    async def tick(tasks, *, timeout, return_when):
        clock[0] += max(timeout, 30)
        await asyncio.sleep(0)
        return set(), set(tasks)

    import time

    monkeypatch.setattr("app.workers.sync.time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr("app.workers.sync.asyncio.wait", tick)
    task = asyncio.create_task(sync.run(stop))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(seen.wait(), 0.5)
        await task
        assert r.store.rows("SELECT id FROM orders") == [{"id": "offline-paid"}]
    finally:
        stop.set()
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_completed_failed_chat_child_is_retired_and_new_orders_keep_advancing(shop):
    r = await shop()
    r.api.order("new")
    sync = Synchronizer(r.store, r.api, Orders(r.store))
    sync._background_chats = True
    failed = asyncio.get_running_loop().create_future()
    failed.set_exception(OSError("failed chat lane"))
    sync._chat_task = failed
    try:
        await sync.once()
        assert r.store.rows("SELECT id FROM orders") == [{"id": "new"}]
        assert sync._chat_task is not failed
    finally:
        if sync._chat_task is not failed:
            sync._chat_task.cancel()
            await asyncio.gather(sync._chat_task, return_exceptions=True)
