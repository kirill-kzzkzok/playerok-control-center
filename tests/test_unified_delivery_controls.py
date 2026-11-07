"""Owner journeys through the production Dispatcher; only API boundaries are simulated."""

from copy import deepcopy

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.gifts.worker import GiftWorker
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.workers.logical_products import LogicalProductWorker
from tests.test_telegram_transport import production_dispatcher
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_universal_delivery_e2e import simulation as simulation_fixture
from tests.test_ux_redesign import buttons


@pytest.fixture
async def shop(tmp_path):
    async for create in simulation_fixture.__wrapped__(tmp_path):
        yield create


def state(r):
    return FSMContext(r.ui.dp.storage, StorageKey(bot_id=r.bot.id, chat_id=7, user_id=7))


async def stop(r):
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":stop")
    await click(r, suffix=":stop_confirm")


@pytest.mark.parametrize("route", ["gft:item:lot", "gft:bind:lot", "gft:enable:lot", "auto:item:lot"])
async def test_old_gift_entries_open_one_control_without_enabling_a_projection(shop, route):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart, heart")
    await stop(r)
    before = r.store.rows("SELECT id,status FROM outbox")
    await r.ui.click(route)
    visible = buttons(r.session.last_panel)
    assert any(key.startswith("lp:") and key.endswith(":arm") for key in visible)
    assert not any(key.startswith(("gft:arm:", "gft:enable:", "ap:")) for key in visible)
    assert (await state(r).get_data()).get("logical_view")
    assert r.store.rows("SELECT enabled FROM logical_products") == [{"enabled": 0}]
    assert r.store.rows("SELECT enabled FROM gift_listings") == [{"enabled": 0}]
    assert r.store.rows("SELECT id,status FROM outbox") == before
    assert not r.api.remote.payments


async def test_old_payment_confirmation_from_durable_fsm_cannot_enable_only_gifts(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    await stop(r)
    listing = r.store.rows("SELECT * FROM gift_listings")[0]
    # Fault injection: a genuine-shaped confirmation left by the previous UI
    # version. It must not gain authority over a newer permanent binding.
    await state(r).set_data(
        {"gift_arm": {"item": "lot", "revision": listing["revision"], "nonce": "previous"}}
    )
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.click("gft:arm:lot:previous:0")
    assert r.store.rows("SELECT enabled FROM gift_listings") == [{"enabled": 0}]
    assert r.store.rows("SELECT enabled FROM logical_products") == [{"enabled": 0}]
    assert "включены только" not in r.session.last_panel.text
    assert not r.api.remote.payments


async def test_old_stop_disables_the_product_and_all_relisted_gift_projections(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    r.api.item["status"] = "SOLD"
    r.api.items["second"] = {**deepcopy(r.api.item), "id": "second", "status": "APPROVED"}
    await LogicalProductWorker(r.store, r.api).once()
    assert len(r.store.rows("SELECT 1 FROM gift_listings WHERE enabled=1")) == 2
    await r.ui.click("gft:off:lot")
    await click(r, suffix=":stop_confirm")
    assert r.store.rows("SELECT enabled FROM logical_products") == [{"enabled": 0}]
    assert not r.store.rows("SELECT 1 FROM gift_listings WHERE enabled=1")
    assert not r.api.remote.payments


async def test_legacy_text_entry_redirects_to_the_permanent_product(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-test-value")
    await r.ui.click("auto:item:lot")
    visible = buttons(r.session.last_panel)
    assert any(key.startswith("lp:") for key in visible)
    assert not any(key.startswith("ap:") for key in visible)
    assert "private-test-value" not in r.session.last_panel.text


async def test_item_delivery_has_one_configuration_entry_and_truthful_stopped_state(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    await stop(r)
    # Inconsistent projection left by an older version is NOT an active product.
    r.store.execute("UPDATE gift_listings SET enabled=1")
    await r.ui.click("item:lot")
    assert "остановлена" in r.session.last_panel.text.lower()
    await r.ui.click("iv:delivery:lot")
    visible = buttons(r.session.last_panel)
    assert "lp:method:lot" in visible
    assert "gft:item:lot" not in visible and "auto:item:lot" not in visible
    assert not r.api.remote.payments


async def test_redirect_then_fresh_confirmation_arms_both_and_delivers_once(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart, heart")
    await stop(r)
    await r.ui.click("gft:enable:lot")
    await click(r, suffix=":arm")
    confirmation = await click(r, suffix=":enable:inherit")
    await r.ui.click(confirmation)
    product = LogicalProducts(r.store, r.api).listing("lot")
    assert product["enabled"] == 1 and product["inherit_enabled"] == 1
    assert r.store.rows("SELECT enabled FROM gift_listings") == [{"enabled": 1}]
    assert not r.api.remote.payments
    Orders(r.store).ingest(r.api.order("new-paid"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.worker.once()
    assert len(r.api.remote.payments) == 2
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    assert not await r.worker.once()


async def test_text_product_keeps_a_read_only_paid_order_check(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-test-value")
    Orders(r.store).ingest(r.api.order("check-order"))
    before = r.store.rows("SELECT id,status FROM outbox")
    await r.ui.click("iv:delivery:lot")
    await click(r, action="lp:details:lot")
    await click(r, action="auto:check:lot")
    await click(r, suffix=":simulate:0")
    assert "Проверка без отправки" in r.session.last_panel.text
    assert not any(key.endswith(":start_chain") for key in buttons(r.session.last_panel))
    assert "order:check-order" in buttons(r.session.last_panel)
    assert r.store.rows("SELECT id,status FROM outbox") == before
    assert not r.api.messages and not r.api.remote.payments


async def test_intentionally_manual_product_is_not_reported_as_broken_automation(shop):
    r = await shop()
    for route in ("items", "item:lot", "iv:delivery:lot"):
        await click(r, action=route)
    await click(r, action="lp:method:lot")
    await click(r, suffix=":mode:manual")
    await click(r, suffix=":preview")
    await click(r, suffix=":commit")
    await r.worker.once()
    await r.ui.click("item:lot")
    assert "Выдача: вручную" in r.session.last_panel.text
    assert "остановлена" not in r.session.last_panel.text


@pytest.mark.parametrize("gifts", [False, True])
async def test_global_stop_also_fences_the_permanent_product_for_a_clean_future_restart(shop, gifts):
    r = await shop(gift=gifts)
    await setup_ui(r, "telegram_gifts" if gifts else "fixed_text", "gift: heart" if gifts else "private-text")
    await r.ui.stop("gft:stop" if gifts else "auto:stop")
    assert r.store.rows("SELECT enabled FROM logical_products") == [{"enabled": 0}]
    await r.ui.click("lp:item:lot")
    assert any(key.endswith(":arm") for key in buttons(r.session.last_panel))
    assert not r.api.remote.payments and not r.api.messages
