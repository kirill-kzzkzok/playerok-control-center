"""Seller-facing routes through real Dispatcher and durable state."""

import pytest

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
