"""Aggregated launch diagnostics through the actual owner UI, no live APIs."""

from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_unified_delivery_controls import stop
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_all_known_text_blockers_are_shown_without_enabling_or_queueing(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-text")
    await stop(r)
    r.api.account.writes_enabled = False
    r.api.account.automation_enabled = False
    r.store.set_setting("sync:health", {"status": "ERROR"})
    before = r.store.rows("SELECT id,status FROM outbox")
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":arm")
    text = r.session.last_panel.text
    assert "Запуск пока невозможен" in text
    assert "Записи Playerok выключены" in text and "Глобальная автоматизация выключена" in text
    assert "синхронизация" in text
    assert not any(":enable:" in key for key in buttons(r.session.last_panel))
    assert r.store.rows("SELECT id,status FROM outbox") == before


async def test_gift_balance_and_global_stop_are_reported_together(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart, heart")
    await stop(r)
    r.store.set_setting("gifts:stopped", True)
    # The external transport is the only substituted layer.
    r.api.remote.stars = 0
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":arm")
    text = r.session.last_panel.text
    assert "Запуск пока невозможен" in text
    assert "общая остановка подарочных покупок" in text
    assert "Недостаточно Stars" in text
    assert not r.api.remote.payments


async def test_item_offers_start_and_relisting_permission_without_claiming_platform_setting(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-text")
    await stop(r)
    await r.ui.click("item:lot")
    visible = buttons(r.session.last_panel)
    # Seller card deliberately has one settings entry; launch and inheritance
    # are configured there, rather than duplicated on the main card.
    assert "iv:delivery:lot" in visible
    assert (
        "[" + r.store.rows("SELECT code FROM logical_products")[0]["code"] + "]" in r.session.last_panel.text
    )
    assert "Перевыставление включено" not in r.session.last_panel.text
    assert "private-text" not in r.session.last_panel.text
    await r.ui.click("iv:delivery:lot")
    await click(r, action="lp:details:lot")
    assert "Наследование перевыставлений" in r.session.last_panel.text
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":arm")
    assert any(":enable:inherit" in key for key in buttons(r.session.last_panel))
    await click(r, suffix=":enable:inherit")
    assert r.store.rows("SELECT enabled,inherit_enabled FROM logical_products") == [
        {"enabled": 1, "inherit_enabled": 1}
    ]
    assert not r.api.messages


async def test_gift_readiness_checks_receiving_field_before_offering_financial_confirmation(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    await stop(r)

    async def missing_field(*args, **kwargs):
        yield []

    r.api.pages = missing_field
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":arm")
    assert "Запуск пока невозможен" in r.session.last_panel.text
    assert not any(":enable:" in key for key in buttons(r.session.last_panel))
    assert not r.api.remote.payments
