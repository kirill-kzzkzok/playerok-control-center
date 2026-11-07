"""Production UI checks for shared per-product delivery controls."""

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from tests.test_universal_delivery_e2e import click
from tests.test_universal_delivery_e2e import simulation as simulation_fixture
from tests.test_ux_redesign import buttons

simulation = simulation_fixture


async def test_logical_stock_input_uses_shared_pending_and_keeps_setup(simulation):
    r = await simulation()
    await click(r, action="items")
    await click(r, action="item:lot")
    await click(r, action="iv:delivery:lot")
    await click(r, action="lp:method:lot")
    await click(r, suffix=":mode:unique_stock")
    await click(r, suffix=":stockinput")
    await r.ui.message("secret-first\nsecret-second")
    state = FSMContext(r.ui.dp.storage, StorageKey(bot_id=r.bot.id, chat_id=7, user_id=7))
    assert (await state.get_data()).get("stock_pending")
    assert "secret-first" not in r.session.last_panel.text
    await click(r, suffix=":stock_commit")
    data = await state.get_data()
    assert data["logical_setup"]["handler"] == "unique_stock"
    assert r.store.rows("SELECT status FROM inventory") == [{"status": "AVAILABLE"}, {"status": "AVAILABLE"}]
    assert not r.store.rows("SELECT * FROM outbox")


async def test_delivery_controls_do_not_offer_enable_and_stop_together(simulation):
    from tests.test_universal_delivery_e2e import setup_ui

    r = await simulation()
    await setup_ui(r, "fixed_text", "private-delivery")
    r.store.execute("UPDATE delivery_policies SET payload=json_set(payload,'$.enabled',json('false'))")
    await r.ui.click("lp:item:lot")
    keys = buttons(r.session.last_panel)
    assert not (any(k.endswith(":stop") for k in keys) and any(k.endswith(":arm") for k in keys))
    assert not r.api.messages


async def test_stop_requires_current_confirmation_and_cancel_is_read_only(simulation):
    from tests.test_universal_delivery_e2e import setup_ui

    r = await simulation()
    await setup_ui(r, "fixed_text", "private-delivery")
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":stop")
    assert "Затронуто объявлений: 1" in r.session.last_panel.text
    confirm = next(k for k in buttons(r.session.last_panel) if k.endswith(":stop_confirm"))
    assert r.store.rows("SELECT enabled FROM logical_products") == [{"enabled": 1}]
    await r.ui.click("lp:item:lot")
    await r.ui.click(confirm)
    assert r.store.rows("SELECT enabled FROM logical_products") == [{"enabled": 1}]
    assert not r.api.messages


async def test_paid_text_order_reports_background_refresh_without_stock_action(simulation):
    from app.services.orders import Orders
    from tests.test_universal_delivery_e2e import setup_ui

    r = await simulation()
    await setup_ui(r, "fixed_text", "private-delivery")
    Orders(r.store).ingest(r.api.order("paid"))
    await r.ui.click("order:paid")
    assert "od:stock:paid" not in buttons(r.session.last_panel)
    assert "Обновление запрошено" in r.session.last_panel.text
    assert r.store.rows("SELECT kind,target FROM sync_requests") == [{"kind": "deal", "target": "paid"}]
    assert not r.api.messages


async def test_shop_stars_limits_cancel_returns_to_settings(simulation):
    r = await simulation(gift=True)
    await r.ui.click("settings")
    await r.ui.click("gft:limits:settings")
    assert buttons(r.session.last_panel).get("settings") == "Отмена"
    await r.ui.click("settings")
    assert not r.api.remote.payments


async def test_stop_confirmation_counts_unique_links_and_rejects_changed_control(simulation):
    from copy import deepcopy

    from app.services.logical_products import LogicalProducts
    from app.workers.logical_products import LogicalProductWorker
    from tests.test_universal_delivery_e2e import setup_ui

    r = await simulation()
    await setup_ui(r, "fixed_text", "private-delivery")
    r.api.item["status"] = "SOLD"
    r.api.items["second"] = {**deepcopy(r.api.item), "id": "second", "status": "APPROVED"}
    await LogicalProductWorker(r.store, r.api).once()
    await r.ui.click("lp:item:second")
    await click(r, suffix=":stop")
    assert "Затронуто объявлений: 2" in r.session.last_panel.text
    confirm = next(k for k in buttons(r.session.last_panel) if k.endswith(":stop_confirm"))
    r.store.execute("UPDATE logical_products SET control_revision=control_revision+1")
    await r.ui.click(confirm)
    assert LogicalProducts(r.store, r.api).listing("second")["enabled"] == 1
    await r.ui.click("lp:item:second")
    await click(r, suffix=":stop")
    confirm = next(k for k in buttons(r.session.last_panel) if k.endswith(":stop_confirm"))
    await r.ui.click(confirm)
    stopped = r.store.rows("SELECT enabled,control_revision FROM logical_products")
    assert stopped[0]["enabled"] == 0
    await r.ui.click(confirm)
    assert r.store.rows("SELECT enabled,control_revision FROM logical_products") == stopped
    assert not r.api.messages


async def test_stale_order_stock_button_cannot_import_into_text_product(simulation):
    from app.services.orders import Orders
    from tests.test_universal_delivery_e2e import setup_ui
    r=await simulation()
    await setup_ui(r,"fixed_text","private-delivery")
    Orders(r.store).ingest(r.api.order("paid"))
    await r.ui.click("od:stock:paid")
    state=FSMContext(r.ui.dp.storage,StorageKey(bot_id=r.bot.id,chat_id=7,user_id=7))
    assert await state.get_state()!="stock:input"
    assert not r.store.rows("SELECT * FROM inventory")
