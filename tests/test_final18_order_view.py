import asyncio
from copy import deepcopy

import pytest

from app.services.orders import Orders
from app.workers.sync import Synchronizer
from tests.test_product_readiness import shop as shop_fixture
from tests.test_telegram_transport import production_dispatcher

shop = shop_fixture


async def test_known_order_opens_locally_and_existing_sync_refreshes_after_restart(shop):
    r = await shop()
    Orders(r.store).ingest(r.api.order("paid"))
    original = r.api.call
    reads = []

    async def slow(method, **kwargs):
        reads.append(method)
        await asyncio.sleep(6)
        return await original(method, **kwargs)

    r.api.call = slow
    for restart in (False, True):
        if restart:
            r.ui.dp = production_dispatcher(r.store, r.api)
        await asyncio.wait_for(r.ui.click("order:paid"), 0.7)
        assert "Сохранённые данные" in r.session.last_panel.text
        assert "Обновлено:" in r.session.last_panel.text
        assert not reads
    pending = r.store.rows("SELECT * FROM sync_requests WHERE key='deal:paid'")
    assert len(pending) == 1 and pending[0]["revision"] > pending[0]["completed_revision"]
    r.api.call = original
    r.api.orders["paid"]["status"] = "SENT"
    await Synchronizer(r.store, r.api, Orders(r.store)).process_requests()
    await r.ui.click("order:paid")
    assert "Продавец отметил отправку" in r.session.last_panel.text
    assert not r.api.messages and not r.api.remote.payments


@pytest.mark.parametrize("conflict", ["seller", "bound", "sdk"])
async def test_foreign_cached_sale_is_never_displayed(shop, conflict):
    r = await shop()
    order = deepcopy(r.api.order("private-order"))
    if conflict == "seller":
        order["item"]["user"]["id"] = "other-account"
    Orders(r.store).ingest(order)
    if conflict == "bound":
        r.store.set_setting("playerok:account_id", "other-account")
    if conflict == "sdk":
        r.api.account.id = "other-account"
    r.api.calls.clear()
    await r.ui.click("order:private-order")
    assert "❗" in r.session.last_panel.text
    assert "private-order" not in r.session.last_panel.text
    assert not r.store.rows("SELECT * FROM sync_requests")
    assert not r.api.messages and not r.api.remote.payments
