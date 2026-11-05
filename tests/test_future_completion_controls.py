import json

import pytest

from app.gifts.worker import GiftWorker
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_logical_faults import reopen
from tests.test_playerok_delivery import configure
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui

shop = shop_fixture


@pytest.mark.parametrize(
    "mode,value", [("unique_stock", None), ("fixed_text", "private-text"), ("telegram_gifts", "gift: heart")]
)
@pytest.mark.parametrize("already_enrolled", [False, True])
async def test_active_completion_change_applies_only_to_future_sales(shop, mode, value, already_enrolled):
    r = await shop(gift=mode == "telegram_gifts")
    await setup_ui(r, mode, value)
    logical = LogicalProducts(r.store, r.api)
    before = logical.listing("lot")
    prior = json.loads(before["config"])
    old_enabled = (
        prior.get("auto_fulfill", False)
        if mode == "telegram_gifts"
        else prior.get("automatic_completion", False)
    )
    Orders(r.store).ingest(r.api.order("old-paid"))
    if already_enrolled:
        await EnrollmentWorker(r.store, r.api).once()
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.ui.click("lp:completion:lot")
    after = logical.listing("lot")
    config = json.loads(after["config"])
    enabled = (
        config.get("auto_fulfill", False)
        if mode == "telegram_gifts"
        else config.get("automatic_completion", False)
    )
    assert enabled is not old_enabled
    assert all(before[k] == after[k] for k in ("id", "revision", "control_revision", "enabled", "armed_at"))
    reopen(r)
    Orders(r.store).ingest(r.api.order("new-paid"))
    for _ in range(18):
        await EnrollmentWorker(r.store, r.api).once()
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert r.api.orders["old-paid"]["status"] == ("SENT" if old_enabled else "PAID")
    assert r.api.orders["new-paid"]["status"] == ("PAID" if old_enabled else "SENT")
    assert len(r.api.remote.payments) == (2 if mode == "telegram_gifts" else 0)


@pytest.mark.parametrize("initial", [True, False])
async def test_platform_completion_change_keeps_old_paid_conditions(shop, initial):
    r = await shop()
    await configure(r, reply=True, fulfill=initial)
    old = r.api.order("old-platform")
    old["is_automated"] = True
    Orders(r.store).ingest(old)
    before = LogicalProducts(r.store, r.api).listing("lot")
    await r.ui.click("platform:item:lot")
    await click(r, suffix=":fulfill")
    await click(r, suffix=":save")
    after = LogicalProducts(r.store, r.api).listing("lot")
    assert all(before[k] == after[k] for k in ("revision", "control_revision", "armed_at"))
    reopen(r)
    new = r.api.order("new-platform")
    new["is_automated"] = True
    Orders(r.store).ingest(new)
    for _ in range(12):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert r.api.orders["old-platform"]["status"] == ("SENT" if initial else "PAID")
    assert r.api.orders["new-platform"]["status"] == ("PAID" if initial else "SENT")
    assert not r.api.remote.payments and not r.store.rows("SELECT * FROM reservations")


async def test_old_scope_without_completion_keys_keeps_reminder_after_future_opt_in(shop):
    r = await shop()
    await setup_ui(r, "unique_stock", legacy_reminder=True)
    Orders(r.store).ingest(r.api.order("legacy-paid"))
    await r.ui.click("lp:completion:lot")
    reopen(r)
    Orders(r.store).ingest(r.api.order("new-automatic"))
    for _ in range(16):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert r.store.rows("SELECT status FROM delivery_chains WHERE order_id='legacy-paid'") == [
        {"status": "NEEDS_FULFILLMENT"}
    ]
    assert r.api.orders["legacy-paid"]["status"] == "PAID"
    assert r.api.orders["new-automatic"]["status"] == "SENT"
