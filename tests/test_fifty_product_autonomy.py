"""Fifty independently owner-configured lots, actual production execution loops."""

import asyncio
from copy import deepcopy
from time import monotonic

from app.__main__ import outbox_loop
from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.sync import Synchronizer
from tests.test_final_outbox_resource_lanes import configure_other_item
from tests.test_logical_faults import reopen
from tests.test_unified_delivery_controls import shop as shop_fixture

shop = shop_fixture


async def test_fifty_independent_lots_keep_delivering_when_one_ack_is_lost(shop, record_property):
    r = await shop()

    def update(id, **changes):
        r.api.mutations.append(deepcopy(changes))
        r.api.items[id].update(changes)
        return deepcopy(r.api.items[id])

    r.api.account.update_item = update
    for index in range(50):
        await configure_other_item(
            r,
            f"lot-{index:02}",
            "unique_stock" if index % 2 else "fixed_text",
            content=f"content-lot-{index:02}",
        )
    assert r.store.rows("SELECT count(*) n FROM logical_products WHERE enabled=1") == [{"n": 50}]
    assert r.store.rows("SELECT count(DISTINCT code) n FROM logical_products") == [{"n": 50}]
    original_read, original_send = r.api.call, r.api.account.send_message
    sent_at, lost_calls = {}, []

    async def read(method, **kwargs):
        await asyncio.sleep(0.005)
        return await original_read(method, **kwargs)

    def send(chat_id, text):
        result = original_send(chat_id, text)
        if text.startswith("content-lot-"):
            sent_at[chat_id] = monotonic()
        if chat_id == "chat-sale-00":
            lost_calls.append(text)
            raise APIError("NETWORK_ERROR", uncertain=True)
        return result

    r.api.call, r.api.account.send_message = read, send
    for index in range(50):
        r.api.order(f"sale-{index:02}", f"lot-{index:02}")
    sync = Synchronizer(r.store, r.api, Orders(r.store))
    await sync.once()
    await sync.once()  # Duplicate server observations cannot multiply admission.
    available = monotonic()
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(EnrollmentWorker(r.store, r.api).run(stop)),
        asyncio.create_task(ChainWorker(r.store, r.api).run(stop)),
        asyncio.create_task(outbox_loop(r.worker, stop)),
    ]
    try:
        async with asyncio.timeout(60):
            while len(r.store.rows("SELECT 1 FROM delivery_chains WHERE status='SUCCEEDED'")) < 49:
                await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.gather(*tasks)
    elapsed = monotonic() - available
    assert elapsed < 60
    assert len(sent_at) == 50
    assert set(sent_at) == {f"chat-sale-{index:02}" for index in range(50)}
    delivered = [(m["chat_id"], m["text"]) for m in r.api.messages if m["text"].startswith("content-lot-")]
    assert sorted(delivered) == [(f"chat-sale-{index:02}", f"content-lot-{index:02}") for index in range(50)]
    assert len(lost_calls) == 1
    assert r.store.rows("SELECT count(*) n FROM inventory WHERE status='DELIVERED'") == [{"n": 25}]
    assert len(r.store.rows("SELECT 1 FROM outbox WHERE status='UNCERTAIN'")) == 1
    assert len(r.store.rows("SELECT 1 FROM logical_order_scopes")) == 50
    before = deepcopy(r.api.messages)
    reopen(r)
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    while await r.worker.once():
        pass
    assert r.api.messages == before and len(lost_calls) == 1
    assert not r.api.remote.payments
    record_property("paid_available_to_49_confirmed_seconds", elapsed)
    record_property("max_transport_dispatch_delay_seconds", max(sent_at.values()) - available)
    record_property("external_read_delay_seconds", 0.005)
