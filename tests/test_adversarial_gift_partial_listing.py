from copy import deepcopy

import pytest

from app.gifts.worker import GiftWorker
from app.services.orders import Orders
from tests.test_logical_faults import reopen
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


@pytest.mark.parametrize("field", ["user", "category", "obtaining_type", "status", "description", "price"])
async def test_incomplete_listing_waits_without_disarming_then_delivers_once(shop, field):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    Orders(r.store).ingest(r.api.order("partial"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    original = r.api.call
    incomplete = True

    async def read(method, **kwargs):
        result = await original(method, **kwargs)
        if incomplete and method == "get_item":
            result = deepcopy(result)
            result.pop(field, None)
        return result

    r.api.call = read
    authority = r.store.rows("SELECT enabled,revision FROM gift_listings")
    await r.worker.once()
    assert not r.api.remote.payments
    assert r.store.rows("SELECT enabled,revision FROM gift_listings") == authority
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE kind='gift_send'") == [
        {"status": "RETRYABLE_FAILED", "started_at": None}
    ]
    incomplete = False
    reopen(r)
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE kind='gift_send'")
    for _ in range(4):
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        await r.worker.once()
    assert len(r.api.remote.payments) == 1
    assert r.store.rows("SELECT status FROM outbox WHERE kind='gift_send'") == [{"status": "SUCCEEDED"}]
