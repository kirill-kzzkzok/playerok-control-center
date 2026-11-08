"""Seller-facing routes through real Dispatcher and durable state."""

import pytest

from app.workers.logical_products import LogicalProductWorker
from tests.test_stage_a_controls import shop as shop_fixture
from tests.test_unified_delivery_controls import state
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_home_has_six_business_entries_with_attention_under_orders(shop):
    r = await shop()
    await r.ui.message("/start")
    expected = {"items", "orders", "chats", "promotions", "stats:7", "settings"}
    assert set(buttons(r.session.last_panel)) == expected
    await r.ui.click("orders")
    assert "attention" in buttons(r.session.last_panel)
    await r.ui.click("settings")
    assert "tools" in buttons(r.session.last_panel)


async def test_complete_active_list_updates_home_count_without_another_request(shop):
    r = await shop()
    await r.ui.click("items")
    reads = len(r.api.calls)
    await r.ui.click("home")
    assert "Активные товары: 1" in r.session.last_panel.text
    assert len(r.api.calls) == reads
    assert not r.store.rows("SELECT * FROM outbox")


async def test_partial_new_catalog_does_not_leave_an_old_total_on_home(shop):
    from app.services.item_filters import refresh_due_catalog

    r = await shop()
    await r.ui.click("items")
    original = r.api.call

    async def partial(method, **kwargs):
        value = await original(method, **kwargs)
        if method == "get_my_items":
            value["page_info"] = {"has_next_page": True, "end_cursor": "next-page"}
        return value

    r.api.call = partial
    before = len(r.api.calls)
    await r.ui.click("items")
    assert len(r.api.calls) == before
    await refresh_due_catalog(r.store, r.api)
    reads = len(r.api.calls)
    await r.ui.click("home")
    assert "Активные товары: обновляем данные" in r.session.last_panel.text
    assert len(r.api.calls) == reads


async def test_background_catalog_completion_restores_total_after_partial_ui_read(shop):
    from app.services.item_filters import refresh_due_catalog

    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    original = r.api.call

    async def partial(method, **kwargs):
        value = await original(method, **kwargs)
        if method == "get_my_items":
            value["page_info"] = {"has_next_page": True, "end_cursor": "next-page"}
        return value

    r.api.call = partial
    before = len(r.api.calls)
    await r.ui.click("items")
    assert len(r.api.calls) == before
    await refresh_due_catalog(r.store, r.api)
    await r.ui.click("home")
    assert "Активные товары: обновляем данные" in r.session.last_panel.text
    r.api.call = original
    await LogicalProductWorker(r.store, r.api).once()
    reads = len(r.api.calls)
    await r.ui.click("home")
    assert "Активные товары: 1" in r.session.last_panel.text
    assert len(r.api.calls) == reads
    assert not r.api.remote.payments and not r.api.messages


@pytest.mark.parametrize("mode", ["telegram_gifts", "unique_stock", "fixed_text"])
async def test_item_has_four_actions_and_opens_existing_delivery_controller(shop, mode):
    r = await shop(gift=mode == "telegram_gifts")
    await setup_ui(r, mode, "gift: heart" if mode == "telegram_gifts" else "Saved delivery text")
    await r.ui.click("item:lot")
    keys = [key for key in buttons(r.session.last_panel) if not key.startswith("nav:") and key != "items"]
    assert set(keys) == {"iv:delivery:lot", "iv:edit:lot", "payp:item:lot", "finance:lot"}
    assert "Полная проверка:" not in r.session.last_panel.text
    assert "ревизи" not in r.session.last_panel.text.lower()
    if mode == "unique_stock":
        assert "Остаток: 3" in r.session.last_panel.text
    await r.ui.click("iv:delivery:lot")
    assert (await state(r).get_data()).get("logical_view", {}).get("item") == "lot"
    assert any(key.endswith(":stop") for key in buttons(r.session.last_panel))
    assert "Настройки текущего способа" not in r.session.last_panel.text
    assert "Сверка каталога:" not in r.session.last_panel.text
    assert "Разрешение выдачи:" not in r.session.last_panel.text
    visible = buttons(r.session.last_panel)
    assert "lp:details:lot" in visible
    assert not any(key.endswith((":detach", ":links", ":check")) for key in visible)
    await r.ui.click("lp:details:lot")
    assert any(key.endswith(":links") for key in buttons(r.session.last_panel))
    assert "rd:check:lot" in buttons(r.session.last_panel)


@pytest.mark.parametrize("configured", [True, False])
async def test_home_distinguishes_playerok_recheck_from_disabled_automation(shop, configured):
    from types import SimpleNamespace

    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    r.api.connection = SimpleNamespace(
        state={"status": "UNKNOWN", "reason": "UNAUTHORIZED_OR_CHALLENGE"},
        desired_writes=True,
        desired_automation=configured,
    )
    r.api.account.writes_enabled = False
    r.api.account.automation_enabled = False
    for key in ("ui:catalog", "shop:active", "logical:scan"):
        suffix = ":completed" if key == "logical:scan" else ""
        r.store.set_setting(f"{key}:{r.api.account.id}{suffix}", {})
    reads = len(r.api.calls)
    await r.ui.click("home")
    text = r.session.last_panel.text
    assert ("запрещён настройками подключения" in text) is not configured
    assert "Проверьте подключение в профиле" not in text
    assert "автоматически перепроверяет подключение Playerok" in text
    assert "Активные товары: обновляем данные" not in text
    assert len(r.api.calls) == reads
