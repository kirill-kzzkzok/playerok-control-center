"""Simulated workload: real SQLite/workers, external transport responses only."""

import asyncio
from copy import deepcopy
from time import perf_counter

import pytest

from app.playerok.adapter import APIError
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.logical_products import LogicalProductWorker
from tests.test_logical_faults import reopen
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


@pytest.mark.parametrize("size", [50, 100])
async def test_catalog_relist_and_ten_buyers_fifty_sales_remain_exact_under_load(shop, size, record_property):
    r = await shop()
    await setup_ui(r, "fixed_text", "delivery-for-correct-product")
    r.store.set_setting("finance:7:cost:lot", "12.34")
    start = perf_counter()
    r.api.item["status"] = "SOLD"
    r.api.items["new"] = {**deepcopy(r.api.item), "id": "new", "status": "APPROVED"}
    for n in range(size - 1):
        r.api.items[f"unrelated-{n}"] = {
            **deepcopy(r.api.item),
            "id": f"unrelated-{n}",
            "description": "Other product",
            "status": "APPROVED",
        }
    call = r.api.call
    failed = False

    async def boundary(method, **kwargs):
        nonlocal failed
        if method == "get_my_items":
            if not failed:
                failed = True
                raise APIError("HTTP_429")
            values = [i for i in r.api.items.values() if i["status"] == "APPROVED"]
            offset, count = int(kwargs.get("after_cursor") or 0), kwargs["count"]
            more = offset + count < len(values)
            return {
                "items": deepcopy(values[offset : offset + count]),
                "page_info": {"has_next_page": more, "end_cursor": str(offset + count) if more else None},
            }
        return await call(method, **kwargs)

    r.api.call = boundary
    worker = LogicalProductWorker(r.store, r.api)
    with pytest.raises(APIError):
        await worker.once()
    reopen(r)
    for _ in range(12):
        await LogicalProductWorker(r.store, r.api).once()
        if LogicalProducts(r.store, r.api).listing("new"):
            break
    assert LogicalProducts(r.store, r.api).listing("new")
    assert r.store.setting("finance:7:cost:new") == "12.34"
    for batch in range(5):

        async def paid(index, batch=batch):
            order = r.api.order(f"sale-{batch}-{index}", "new")
            order["user"]["id"] = f"buyer-{index}"
            Orders(r.store).ingest(order)
            Orders(r.store).ingest(order)

        await asyncio.gather(*(paid(index) for index in range(10)))
        if batch == 2:
            reopen(r)
        for _ in range(35):
            await EnrollmentWorker(r.store, r.api).once()
            ChainWorker(r.store, r.api).once()
            await r.worker.once()
    deliveries = [m for m in r.api.messages if m["text"] == "delivery-for-correct-product"]
    assert len(deliveries) == 50
    assert len({m["chat_id"] for m in deliveries}) == 50
    assert all(m["chat_id"].startswith("chat-sale-") for m in deliveries)
    assert r.store.rows("SELECT COUNT(*) AS n FROM logical_order_scopes") == [{"n": 50}]
    assert not r.store.rows("SELECT 1 FROM outbox WHERE status='UNCERTAIN'")
    record_property("simulation_seconds", perf_counter() - start)
    record_property("real_api_requests", 0)
