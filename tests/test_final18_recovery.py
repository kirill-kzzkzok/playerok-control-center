import sqlite3

import pytest
from aiogram import Bot

from app.services.promotion_settings import PromotionSettings
from tests.test_paid_promotions import rig as rig_fixture
from tests.test_telegram_integration import Interaction, TelegramSession
from tests.test_telegram_transport import production_dispatcher
from tests.test_ux_redesign import buttons

rig = rig_fixture


@pytest.mark.parametrize("context", ["promotions", "gft:home", "payp:item:item", "ops:list:ALL:0"])
async def test_expired_action_returns_to_its_existing_safe_context(rig, context):
    r = rig
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, production_dispatcher(r.store, r.api))
    try:
        await ui.message("/start")
        await ui.click(context)
        assert "❗" not in bot.session.last_panel.text
        await ui.click("pp:expired:save")
        assert "❗" in bot.session.last_panel.text
        assert context in buttons(bot.session.last_panel)
        assert not any(
            k.startswith(("payp:commit:", "payp:approve:", "payp:confirmarm:"))
            for k in buttons(bot.session.last_panel)
        )
        assert not r.api.charges and not r.store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()


async def test_sqlite_save_failure_preserves_input_policy_and_safe_parent(rig, monkeypatch):
    r = rig
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, production_dispatcher(r.store, r.api))
    try:
        await ui.message("/start")
        await ui.click("promotions")
        await ui.click("promo:budget")
        before = r.settings.get()

        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("simulated disk full")

        monkeypatch.setattr(PromotionSettings, "save", fail)
        await ui.message("50")
        assert r.settings.get() == before
        save = next(k for k in buttons(bot.session.last_panel) if k.endswith(":save"))
        await ui.click(save)
        assert "Не удалось сохранить" in bot.session.last_panel.text
        assert "promotions" in buttons(bot.session.last_panel)
        assert r.settings.get() == before
        assert not any(
            m.__api_method__ == "deleteMessage" and m.message_id == ui.counter for m in bot.session.calls
        )
        assert not r.api.charges and not r.store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()
