import asyncio
import json
from time import perf_counter

import pytest

from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.telegram.item_panel import ItemPanel
from app.workers.sync import Synchronizer
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize("delay", [3, 6])
async def test_local_product_delivery_stock_do_not_wait_for_slow_playerok(shop, delay, record_property):
    r = await shop()
    await setup_ui(r, "unique_stock")
    await ItemPanel(r.api, r.store).owned("lot")
    original = r.api.call
    reads = []

    async def slow(method, **kwargs):
        reads.append(method)
        await asyncio.sleep(delay)
        return await original(method, **kwargs)

    r.api.call = slow
    measures = []
    for route in ["item:lot", "lp:item:lot", "stock:view:lot", "home", "settings"] * 4:
        started = perf_counter()
        await asyncio.wait_for(r.ui.click(route), 0.7)
        measures.append(perf_counter() - started)
    assert not reads
    record_property(
        "local_views_ms",
        json.dumps({"delay": delay, "p50": sorted(measures)[10] * 1000, "p95": sorted(measures)[18] * 1000}),
    )
    assert not r.api.messages and not r.api.remote.payments


async def test_stock_import_and_unit_contents_work_offline_without_rearming(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    await ItemPanel(r.api, r.store).owned("lot")
    before = r.store.rows("SELECT enabled,control_revision,revision FROM logical_products")

    async def offline(*args, **kwargs):
        raise APIError("NETWORK_ERROR")

    r.api.call = offline
    await r.ui.click("stock:view:lot")
    assert "Склад" in r.session.last_panel.text
    await click(r, action="stock:add:lot")
    await r.ui.message("offline-secret-1\noffline-secret-2")
    await click(r, action=next(k for k in buttons(r.session.last_panel) if k.startswith("stock:commit:")))
    assert "Добавлено: 2" in r.session.last_panel.text
    await r.ui.click("stock:list:lot:AVAILABLE:0")
    unit = next(k for k in buttons(r.session.last_panel) if k.startswith("stock:unit:"))
    await r.ui.click(unit)
    assert "isolated-unit-" in r.session.last_panel.text or "offline-secret-" in r.session.last_panel.text
    assert r.store.rows("SELECT enabled,control_revision,revision FROM logical_products") == before
    assert not r.api.messages and not r.api.remote.payments
    await r.ui.click(unit, owner=8)
    assert not r.api.messages


async def test_empty_stock_omits_zero_categories_but_retains_history_when_present(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "text")
    await r.ui.click("stock:view:lot")
    assert "📦 Склад пуст" in r.session.last_panel.text
    assert not any(k.startswith("stock:list:") for k in buttons(r.session.last_panel))


async def test_sync_button_enqueues_existing_catchup_without_waiting_for_network(shop, monkeypatch):
    import tests.test_telegram_integration as integration
    from tests.test_telegram_transport import production_dispatcher

    r = await shop()
    synchronizer = Synchronizer(r.store, r.api, Orders(r.store))
    router = integration.create_router
    monkeypatch.setattr(
        integration,
        "create_router",
        lambda store, api, products, _: router(store, api, products, synchronizer),
    )
    r.ui.dp = production_dispatcher(r.store, r.api)
    original = r.api.call
    reads = []

    async def slow(method, **kwargs):
        reads.append(method)
        await asyncio.sleep(6)
        return await original(method, **kwargs)

    r.api.call = slow
    await asyncio.wait_for(r.ui.click("sync"), 0.5)
    assert not reads
    assert r.store.rows("SELECT kind FROM sync_requests WHERE revision>completed_revision") == [
        {"kind": "catchup"}
    ]
    await asyncio.wait_for(r.ui.click("home"), 0.5)
    r.api.call = original
    await synchronizer.process_requests()
    assert not r.store.rows("SELECT kind FROM sync_requests WHERE revision>completed_revision")


async def test_local_display_refuses_cached_item_after_account_change(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    await ItemPanel(r.api, r.store).owned("lot")
    r.api.account.id = "different-account"
    await r.ui.click("stock:view:lot")
    assert "isolated-unit" not in r.session.last_panel.text
    assert any(
        word in r.session.last_panel.text for word in ("не подтверждён", "изменился", "не принадлежит")
    )
