"""Real Dispatcher journeys for the final single-editor follow-up."""

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from tests.test_paid_promotions import rig as rig_fixture
from tests.test_telegram_integration import Interaction, TelegramSession
from tests.test_telegram_transport import production_dispatcher

rig = rig_fixture


@pytest.mark.parametrize("entry", ["quick", "advanced"])
async def test_daily_budget_has_one_staged_save_without_changing_other_limits(rig, entry):
    r = rig
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, production_dispatcher(r.store, r.api))
    state = FSMContext(ui.dp.storage, StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.message("/start")
        before = r.settings.get()
        if entry == "quick":
            await ui.click("promo:budget")
        else:
            await ui.click("promo:settings")
            nonce = (await state.get_data())["promotion"]["nonce"]
            await ui.click(f"pp:{nonce}:group:budget")
            nonce = (await state.get_data())["promotion"]["nonce"]
            await ui.click(f"pp:{nonce}:edit:daily")
        assert (await state.get_data())["promotion"]["field"] == "daily"
        await ui.message("50")
        assert r.settings.get() == before
        nonce = (await state.get_data())["promotion"]["nonce"]
        await ui.click(f"pp:{nonce}:save")
        revision, policy, selection = r.settings.get()
        assert revision == before[0] + 1 and selection == before[2]
        assert policy.daily == "50.00"
        for field in ("per_operation", "monthly", "max_operations_per_item", "enabled"):
            assert getattr(policy, field) == getattr(before[1], field)
        assert not r.api.charges and not r.store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()


async def test_cancel_promotion_enable_leaves_policy_and_authority_untouched(rig):
    r = rig
    r.settings.stop(7)
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, production_dispatcher(r.store, r.api))
    try:
        await ui.message("/start")
        before = r.settings.get()
        authority = r.store.setting("promotions:paid_authority")
        await ui.click("promo:enable")
        assert r.settings.get() == before
        await ui.click("promotions")
        assert r.settings.get() == before
        assert r.store.setting("promotions:paid_authority") == authority
        assert not r.api.charges and not r.store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()
