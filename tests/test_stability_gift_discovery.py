"""Bounded discovery must progress past ineligible historical paid orders."""

import pytest

from app.gifts.worker import GiftWorker
from app.services.orders import Orders
from tests.test_stability_deal_completeness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui


@pytest.fixture
async def shop(tmp_path):
    async for create in shop_fixture.__wrapped__(tmp_path):
        yield create


async def test_hundred_historical_paid_orders_do_not_starve_new_gift_sale(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    for index in range(100):
        old = r.api.order(f"a-old-{index:03}")
        old["created_at"] = "2020-01-01T00:00:00Z"
        Orders(r.store).ingest(old)
    Orders(r.store).ingest(r.api.order("z-new"))
    for _ in range(5):
        # New worker instances exercise persisted progress across restarts.
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        await r.worker.once()
    assert r.store.rows("SELECT order_id,state FROM gift_jobs") == [
        {"order_id": "z-new", "state": "SUCCEEDED"}
    ]
    assert len(r.api.remote.payments) == 1


async def test_new_lower_id_after_cursor_wrap_restart_and_repeated_sync_is_not_lost(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    Orders(r.store).ingest(r.api.order("z-first"))
    worker = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    assert worker.service.discover() == 1
    assert r.store.setting("gifts:discovery_cursor:seller") == "z-first"
    fresh = r.api.order("a-later")
    Orders(r.store).ingest(fresh)
    Orders(r.store).ingest(fresh)
    historical = r.api.order("a-historical")
    historical["created_at"] = "2020-01-01T00:00:00Z"
    Orders(r.store).ingest(historical)
    for _ in range(6):
        # Recreate worker to prove progress does not rely on in-memory cursor.
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        await r.worker.once()
    assert r.store.rows("SELECT order_id,state FROM gift_jobs ORDER BY order_id") == [
        {"order_id": "a-later", "state": "SUCCEEDED"},
        {"order_id": "z-first", "state": "SUCCEEDED"},
    ]
    assert len(r.api.remote.payments) == 2
    Orders(r.store).ingest(fresh)
    for _ in range(3):
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        await r.worker.once()
    assert len(r.api.remote.payments) == 2
