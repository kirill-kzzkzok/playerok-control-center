import asyncio
from copy import deepcopy

from app.__main__ import outbox_loop
from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.services.paid_promotions import PaidPromotions
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_final_outbox_resource_lanes import configure_other_item
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


async def test_slow_premium_preflight_does_not_starve_paid_stock_and_text(shop):
    r = await shop()

    def update(id, **changes):
        r.api.items[id].update(changes)
        return deepcopy(r.api.items[id])

    r.api.account.update_item = update
    await setup_ui(r, "unique_stock")
    await configure_other_item(r, "text", "fixed_text")
    r.api.items["premium"] = {**deepcopy(r.api.item), "id": "premium", "priority": "PREMIUM", "raw_price": 90}
    original = r.api.call
    slow = False
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(method, **kwargs):
        if method == "get_item_priority_statuses":
            return [{"id": "premium-quote", "type": "PREMIUM", "price": 9, "period": 7}]
        if method == "get":
            return {"id": "seller", "profile": {"balance": {"available": 100}}}
        if method == "get_item" and kwargs["id"] == "premium" and slow:
            entered.set()
            await release.wait()
            raise APIError("NETWORK_ERROR")
        return await original(method, **kwargs)

    r.api.call = read
    paid = PaidPromotions(r.store, r.api)
    operation = await paid.queue(7, await paid.preview(7, "premium", "premium-quote"), "one", manual=True)
    slow = True
    stop = asyncio.Event()
    runner = asyncio.create_task(outbox_loop(r.worker, stop))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        for ident, item in (("stock", "lot"), ("text", "text")):
            Orders(r.store).ingest(r.api.order(ident, item))
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        async with asyncio.timeout(4):
            # The transport thread appends the message before its response is
            # consumed. Await the durable receipt too, not an in-flight send.
            while not (
                r.store.rows("SELECT 1 FROM inventory WHERE status='DELIVERED'")
                and any(m["text"] == "isolated-fixed-text" for m in r.api.messages)
            ):
                await asyncio.sleep(0.02)
        assert not release.is_set()
        assert len(r.store.rows("SELECT 1 FROM inventory WHERE status='DELIVERED'")) == 1
        assert sum(m["text"] == "isolated-fixed-text" for m in r.api.messages) == 1
        assert r.store.rows("SELECT started_at FROM outbox WHERE id=?", (operation,)) == [
            {"started_at": None}
        ]
    finally:
        stop.set()
        release.set()
        await asyncio.wait_for(runner, 5)
