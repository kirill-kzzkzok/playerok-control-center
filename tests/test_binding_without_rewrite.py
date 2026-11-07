"""Explicit owner reconfiguration confirms existing server text without rewriting it."""

import pytest

from app.playerok.adapter import APIError
from app.services.logical_products import LogicalProducts
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_unified_delivery_controls import stop
from tests.test_universal_delivery_e2e import click, setup_ui

shop = shop_fixture


@pytest.mark.parametrize("fault", [None, "changed", "unavailable", "lease"])
async def test_confirm_already_present_binding_never_rewrites_live_listing(shop, fault):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    product = LogicalProducts(r.store, r.api).listing("lot")
    await stop(r)
    r.api.item["description"] = "Updated public description for the approved bear"
    await r.ui.click("lp:item:lot")
    await click(r, action="lp:method:lot")
    await click(r, suffix=":mode:telegram_gifts")
    await click(r, suffix=":giftpick:bear")
    await click(r, suffix=":giftdone")
    await click(r, suffix=":preview")
    await click(r, suffix=":commit")
    before = len(r.api.mutations)
    original, reads = r.api.call, []

    async def boundary(method, **kwargs):
        if method == "get_item":
            reads.append(method)
            if len(reads) == 2:
                if fault == "changed":
                    r.api.item["description"] += " changed concurrently"
                if fault == "unavailable":
                    raise APIError("NETWORK_ERROR")
                if fault == "lease":
                    r.store.execute("UPDATE outbox SET lease_until=0 WHERE status='PROCESSING'")
        return await original(method, **kwargs)

    r.api.call = boundary
    await r.worker.once()
    assert len(r.api.mutations) == before
    current = LogicalProducts(r.store, r.api).listing("lot")
    assert current["id"] == product["id"] and current["code"] == product["code"]
    assert not current["enabled"] and not r.api.remote.payments
    if fault is None:
        assert current["state"] == "VERIFIED"
        LogicalProducts(r.store, r.api).verify_listing(current, r.api.item)
        assert r.store.rows("SELECT status,started_at FROM outbox ORDER BY created_at DESC LIMIT 1") == [
            {"status": "SUCCEEDED", "started_at": None}
        ]
    else:
        assert current["state"] != "VERIFIED"
    assert not r.api.messages
