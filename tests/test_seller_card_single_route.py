"""Compact entry card routes to existing configuration without duplicate menus."""

from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


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
