"""Draft paid placement must prepare delivery in the same launch as free publication."""

import pytest
from aiogram import Bot

from app.services.products import ValidationError
from tests.test_paid_promotions import rig as paid_fixture
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


@pytest.fixture
async def paid(tmp_path):
    async for value in paid_fixture.__wrapped__(tmp_path):
        yield value


async def test_direct_paid_draft_queue_cannot_bypass_launch_or_reserve_money(paid):
    r = paid
    preview = await r.service.preview(7, "item", "premium")
    with pytest.raises(ValidationError):
        await r.service.queue(7, preview, "old-premium", manual=True)
    assert not r.store.rows("SELECT * FROM promotion_expenses")
    assert not r.store.rows("SELECT * FROM outbox")
    assert not r.api.charges


async def test_old_paid_draft_entry_opens_launch_without_expense(paid):
    r = paid
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, dispatcher(r.store, r.api))
    try:
        await ui.click("payp:item:item")
        assert r.store.rows("SELECT 1 FROM drafts WHERE json_extract(payload,'$.launch_target')='item'")
        assert not r.store.rows("SELECT * FROM outbox")
        assert not r.store.rows("SELECT * FROM promotion_expenses")
    finally:
        await bot.session.close()


async def test_stale_paid_draft_confirmation_opens_launch_without_spending(paid):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from app.telegram.storage import SQLiteStorage

    r = paid
    value = await r.service.preview(7, "item", "premium")
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await state.set_data({"paid_pending": {"nonce": "old-paid", "value": value}})
        await ui.click("payp:commit:old-paid")
        rows = r.store.rows("SELECT json_extract(payload,'$.publication') AS publication FROM drafts")
        assert rows == [{"publication": "PREMIUM"}]
        assert not r.store.rows("SELECT * FROM outbox")
        assert not r.store.rows("SELECT * FROM promotion_expenses")
        assert not r.api.charges
    finally:
        await bot.session.close()
