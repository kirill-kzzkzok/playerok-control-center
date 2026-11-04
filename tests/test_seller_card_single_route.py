"""Compact entry card routes to existing configuration without duplicate menus."""

import pytest

from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize("owner_stopped", [False, True])
async def test_closed_ordinary_edit_recovers_through_ui_without_becoming_success_or_ignoring_stop(
    shop, owner_stopped
):
    from app.gifts.worker import GiftWorker
    from app.services.orders import Orders
    from app.services.product_readiness import ProductReadiness
    from tests.test_unified_delivery_controls import state, stop

    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    if owner_stopped:
        await stop(r)
    ident = r.store.enqueue("closed-edit", "edit", "lot", {"name": r.api.item["name"]}, actor=7)
    action = r.store.claim(operation_id=ident)
    r.store.mark_started(action)
    r.store.finish(ident, action["lease_token"], "UNCERTAIN", error="EDIT_NOT_CONFIRMED")
    before = r.store.rows("SELECT enabled FROM logical_products")
    before_gifts = r.store.rows("SELECT enabled FROM gift_listings")
    await r.ui.click("ops:stop:" + ident)
    await r.ui.message("Объявление проверено, закрываю запись без повторной правки")
    data = (await state(r).get_data())["resolution"]
    await r.ui.click("ops:apply:" + data["nonce"])
    assert "Товар этой кнопкой не остановлен" in r.session.last_panel.text
    assert r.store.rows("SELECT enabled FROM logical_products") == before
    assert r.store.rows("SELECT enabled FROM gift_listings") == before_gifts
    assert r.store.rows("SELECT status,error_code FROM outbox WHERE id=?", (ident,)) == [
        {"status": "PERMANENT_FAILED", "error_code": "MANUALLY_STOPPED_UNCERTAIN"}
    ]
    unverified = ProductReadiness(r.store, r.api).snapshot("lot")
    assert not unverified["ready"]
    assert any(p["code"] == "CLOSED_EDIT_RECHECK" for p in unverified["problems"])
    await r.ui.click("ops:show:" + ident)
    assert "Запись правки закрыта владельцем" in r.session.last_panel.text
    assert "rd:check:lot" in buttons(r.session.last_panel)
    await r.ui.click("rd:check:lot")
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    assert not any(p["code"] == "UNCERTAIN" for p in result["problems"])
    if owner_stopped:
        assert result["state"] == "OFF"
        await r.ui.click("iv:delivery:lot")
        await click(r, suffix=":arm")
        await click(r, suffix=":enable:inherit")
    else:
        assert result["ready"], result["problems"]
    Orders(r.store).ingest(r.api.order("new-after-close"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    assert len(r.api.remote.payments) == 1
    assert not await r.worker.once()


async def test_closed_edit_cannot_authorize_changed_delivery_identity(shop):
    from app.gifts.worker import GiftWorker
    from app.services.orders import Orders
    from app.services.product_readiness import ProductReadiness
    from app.services.reconciliation import Reconciliation, signature

    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    ident = r.store.enqueue("closed-critical", "edit", "lot", {"name": r.api.item["name"]}, actor=7)
    action = r.store.claim(operation_id=ident)
    r.store.mark_started(action)
    r.store.finish(ident, action["lease_token"], "UNCERTAIN", error="EDIT_NOT_CONFIRMED")
    service = Reconciliation(r.store, r.api)
    await service.resolve(
        7,
        ident,
        signature(service.action(7, ident)),
        "STOP_UNCERTAIN",
        "Закрываю запись без подтверждения изменения",
    )
    r.api.item["description"] = "Changed outside the confirmed delivery contract"
    await r.ui.click("rd:check:lot")
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    assert not result["ready"]
    assert any(p["code"] == "ITEM_CHANGED" for p in result["problems"])
    Orders(r.store).ingest(r.api.order("blocked-after-close"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert not r.api.remote.payments
    assert not r.store.rows("SELECT 1 FROM gift_jobs WHERE state='SUCCEEDED'")


async def test_main_unknown_edit_offers_exact_readonly_resolution_without_menu_search(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    ident = r.store.enqueue("unknown-edit", "edit", "lot", {"name": r.api.item["name"]}, actor=7)
    action = r.store.claim(operation_id=ident)
    r.store.mark_started(action)
    r.store.finish(ident, action["lease_token"], "UNCERTAIN", error="EDIT_NOT_CONFIRMED")
    await r.ui.click("item:lot")
    assert "ops:show:" + ident in buttons(r.session.last_panel)
    await click(r, action="ops:show:" + ident)
    assert "ops:confirm:" + ident in buttons(r.session.last_panel)
    await click(r, action="ops:confirm:" + ident)
    assert "нет сохранённого подтверждения" in r.session.last_panel.text
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "UNCERTAIN"}]
    assert not r.api.messages and not r.api.remote.payments


async def test_main_unknown_gift_links_to_gift_reconcile_without_second_payment(shop):
    from app.gifts.worker import GiftWorker
    from app.services.orders import Orders

    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    Orders(r.store).ingest(r.api.order("paid-unknown"))
    r.api.remote.pay_error = TimeoutError()
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "UNCERTAIN"}]
    count = len(r.api.remote.payments)
    await r.ui.click("item:lot")
    await click(r, action="gft:details:paid-unknown")
    assert "gft:reconcile:paid-unknown" in buttons(r.session.last_panel)
    await r.ui.click("gft:reconcile:paid-unknown")
    assert "Достоверного подтверждения пока нет" in r.session.last_panel.text
    assert len(r.api.remote.payments) == count == 1
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "UNCERTAIN"}]
    assert not await r.worker.once()


async def test_gift_main_card_has_single_settings_and_readiness_route(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    before = r.store.rows("SELECT * FROM outbox")
    await r.ui.click("item:lot")
    shown = r.session.last_panel
    visible = buttons(shown)
    assert "rd:check:lot" in visible and "iv:delivery:lot" in visible
    assert not {"rd:balance:lot", "iv:messages:lot", "iv:completion:lot", "lp:ready:lot"} & visible.keys()
    assert "Полная проверка:" not in shown.text and "Себестоимость:" not in shown.text
    await r.ui.click("iv:delivery:lot")
    visible = buttons(r.session.last_panel)
    assert any(key.endswith(":configure") for key in visible)
    assert any(key.endswith(":editafter") for key in visible)
    assert "lp:method:lot" in visible
    assert not any(key.startswith(("rd:check:", "rd:balance:")) for key in visible)
    assert "Состав, сообщения и лимиты" not in visible.values()
    await click(r, suffix=":stop")
    await r.ui.click("iv:delivery:lot")
    await click(r, suffix=":configure")
    await click(r, suffix=":mode:telegram_gifts")
    assert any(key.endswith(":giftclear") for key in buttons(r.session.last_panel))
    assert r.store.rows("SELECT * FROM outbox") == before
    assert not r.api.messages and not r.api.remote.payments


async def test_platform_direct_settings_returns_to_item_without_menu_loop(shop):
    from tests.test_playerok_delivery import configure

    r = await shop()
    await configure(r)
    before = r.store.rows("SELECT * FROM outbox")
    await r.ui.click("item:lot")
    await r.ui.click("iv:delivery:lot")
    visible = buttons(r.session.last_panel)
    assert "lp:method:lot" in visible
    back = next(key for key in visible if key.startswith("nav:back:"))
    assert "iv:delivery:lot" not in visible
    assert "rd:check:lot" not in visible
    await r.ui.click(back)
    assert "rd:check:lot" in buttons(r.session.last_panel)
    assert r.store.rows("SELECT * FROM outbox") == before
    assert not r.api.messages


async def test_compact_main_keeps_all_problems_in_existing_full_diagnostic(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    r.store.set_setting("gifts:balance", {"stars": 1, "at": "2020-01-01T00:00:00Z"})
    await r.ui.click("item:lot")
    text = r.session.last_panel.text
    assert text.count("• ") <= 1
    assert "Остальные причины" in text
    await r.ui.click("rd:check:lot")
    assert "Проверка готовности" in r.session.last_panel.text
    assert "Полная проверка:" in r.session.last_panel.text
    assert not r.api.messages and not r.api.remote.payments
