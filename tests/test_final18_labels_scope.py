from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from tests.test_paid_promotions import rig as rig_fixture
from tests.test_telegram_integration import Interaction, TelegramSession
from tests.test_telegram_transport import production_dispatcher
from tests.test_ux_redesign import buttons

rig = rig_fixture


async def test_advanced_promotion_toggle_is_explicitly_unsaved_and_never_arms(rig):
    r = rig
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, production_dispatcher(r.store, r.api))
    try:
        before = r.settings.get()
        await ui.message("/start")
        await ui.click("promotions")
        await ui.click(next(k for k in buttons(bot.session.last_panel) if k.endswith(":advanced")))
        assert "до сохранения" in bot.session.last_panel.text
        labels = buttons(bot.session.last_panel)
        assert "Переключить политику" not in labels.values()
        toggle = next(k for k in labels if k.endswith(":toggle:enabled"))
        assert "в форме" in labels[toggle]
        await ui.click(toggle)
        state = FSMContext(ui.dp.storage, StorageKey(bot_id=123, chat_id=7, user_id=7))
        form = (await state.get_data())["promotion"]
        assert form["values"]["enabled"] != before[1].enabled
        assert r.settings.get() == before
        assert not r.store.rows("SELECT * FROM outbox") and not r.api.charges
    finally:
        await bot.session.close()


async def test_launch_global_input_is_explicit_and_cancel_preserves_original_draft(rig):
    r = rig
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, production_dispatcher(r.store, r.api))
    try:
        await ui.message("/start")
        await ui.click("payp:item:item")
        await ui.click(next(k for k in buttons(bot.session.last_panel) if k.endswith(":advanced")))
        choices = buttons(bot.session.last_panel)
        budget = next(k for k in choices if k.endswith(":edit:budget"))
        assert "всего магазина" in choices[budget]
        state = FSMContext(ui.dp.storage, StorageKey(bot_id=123, chat_id=7, user_id=7))
        ident = (await state.get_data())["launch"]["id"]
        before = r.store.rows("SELECT payload FROM drafts WHERE id=?", (ident,))
        policy = r.settings.get()
        await ui.click(budget)
        assert "всего магазина" in bot.session.last_panel.text
        await ui.message("30 90 300 10")
        assert "ко всем товарам" in bot.session.last_panel.text
        assert r.settings.get() == policy
        await ui.click("launch:open:" + ident)
        assert r.store.rows("SELECT payload FROM drafts WHERE id=?", (ident,)) == before
        assert r.settings.get() == policy
        assert not r.api.charges and not r.store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()
