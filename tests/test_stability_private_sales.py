"""A sold private purchase may prove identity without a public item route."""

from copy import deepcopy

import pytest

from app.gifts.worker import GiftWorker
from app.playerok.adapter import APIError
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.logical_products import LogicalProductWorker
from tests.test_stability_deal_completeness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui


@pytest.fixture
async def shop(tmp_path):
    async for create in shop_fixture.__wrapped__(tmp_path):
        yield create


@pytest.mark.parametrize(
    "mode,value", [("fixed_text", "private-unit"), ("unique_stock", None), ("telegram_gifts", "gift: heart")]
)
async def test_sale_before_scan_with_public_404_delivers_once_from_verified_deal(shop, mode, value):
    r = await shop(gift=mode == "telegram_gifts")
    await setup_ui(r, mode, value)
    original_product = LogicalProducts(r.store, r.api).listing("lot")
    r.api.item["status"] = "SOLD"
    r.api.items["private"] = {**deepcopy(r.api.item), "id": "private", "status": "SOLD", "price": 90}
    Orders(r.store).ingest(r.api.order("fast-sale", item_id="private"))
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_item" and kwargs["id"] == "private":
            raise APIError("HTTP_404")
        return await original(method, **kwargs)

    r.api.call = call
    await LogicalProductWorker(r.store, r.api).once()
    linked = LogicalProducts(r.store, r.api).listing("private")
    assert linked and linked["id"] == original_product["id"]
    assert linked["source_order_id"] == "fast-sale"
    for _ in range(8):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        await r.worker.once()
    if mode == "telegram_gifts":
        assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
        assert len(r.api.remote.payments) == 1
    else:
        assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "SUCCEEDED"}]
        assert len(r.api.messages) == 2
        if mode == "unique_stock":
            assert r.store.rows("SELECT item_id,status FROM inventory WHERE status='DELIVERED'") == [
                {"item_id": "lot", "status": "DELIVERED"}
            ]
    messages, payments = deepcopy(r.api.messages), len(r.api.remote.payments)
    Orders(r.store).ingest(r.api.order("other-sale", item_id="private"))
    for _ in range(3):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        await r.worker.once()
    assert r.api.messages == messages and len(r.api.remote.payments) == payments


async def test_conflicting_full_purchase_price_cannot_adopt_private_snapshot(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-unit")
    r.api.item["status"] = "SOLD"
    r.api.items["private"] = {**deepcopy(r.api.item), "id": "private", "status": "SOLD", "price": 90}
    Orders(r.store).ingest(r.api.order("conflict", item_id="private"))
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_item" and kwargs["id"] == "private":
            raise APIError("HTTP_404")
        value = await original(method, **kwargs)
        if method == "get_delivery_deal":
            value["item"]["price"] = 91
        return value

    r.api.call = call
    await LogicalProductWorker(r.store, r.api).once()
    assert LogicalProducts(r.store, r.api).listing("private") is None
    assert not r.store.rows("SELECT 1 FROM delivery_chains")
    assert not r.api.messages
