"""Future native relists do not require stopping already authorized current sales."""

from copy import deepcopy

import pytest

from app.database.store import Conflict
from app.gifts.worker import GiftWorker
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.logical_products import LogicalProductWorker
from tests.test_stage_a_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui

shop = shop_fixture


@pytest.mark.parametrize("handler,value", [("telegram_gifts", "gift: heart"), ("unique_stock", None)])
async def test_active_inheritance_change_preserves_paid_job_and_original_admission(shop, handler, value):
    r = await shop(gift=handler == "telegram_gifts")
    await setup_ui(r, handler, value)
    logical = LogicalProducts(r.store, r.api)
    original = logical.listing("lot")
    logical.stop(7, original["id"])
    await logical.arm(7, original["id"], original["revision"], inherit=False)
    before = logical.listing("lot")
    Orders(r.store).ingest(r.api.order("pending-before-change"))
    if handler == "telegram_gifts":
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    else:
        await EnrollmentWorker(r.store, r.api).once()
    # An existing saved callback still goes through actual Dispatcher/FSM approval.
    await r.ui.click("lp:ready:lot")
    await click(r, suffix=":enable:inherit")
    current = logical.listing("lot")
    assert current["inherit_enabled"] == 1
    for field in ("armed_at", "control_revision", "config", "revision", "enabled", "id"):
        assert current[field] == before[field]
    for _ in range(6):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    r.api.item["status"] = "SOLD"
    r.api.items["new"] = {**deepcopy(r.api.item), "id": "new", "status": "APPROVED"}
    await LogicalProductWorker(r.store, r.api).once()
    assert logical.listing("new")["id"] == before["id"]
    Orders(r.store).ingest(r.api.order("next-native-sale", "new"))
    for _ in range(6):
        if handler == "telegram_gifts":
            await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        else:
            await EnrollmentWorker(r.store, r.api).once()
            ChainWorker(r.store, r.api).once()
        await r.worker.once()
    if handler == "telegram_gifts":
        assert len(r.api.remote.payments) == 2
    else:
        assert len(r.store.rows("SELECT 1 FROM inventory WHERE status='DELIVERED'")) == 2
    assert len(r.store.rows("SELECT 1 FROM logical_order_scopes")) == 2


async def test_active_inheritance_never_overrides_stop_arriving_during_fresh_read(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    logical = LogicalProducts(r.store, r.api)
    before = logical.listing("lot")
    original, reads = r.api.call, []

    async def boundary(method, **kwargs):
        value = await original(method, **kwargs)
        if method == "get_item":
            reads.append(method)
            if len(reads) == 2:
                logical.stop(7, before["id"])
        return value

    r.api.call = boundary
    with pytest.raises(Conflict):
        await logical.arm(7, before["id"], before["revision"], inherit=False)
    after = logical.listing("lot")
    assert not after["enabled"] and after["inherit_enabled"] == before["inherit_enabled"]
    assert not r.store.rows("SELECT 1 FROM gift_listings WHERE enabled=1")
    assert not r.api.remote.payments
