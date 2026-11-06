import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.telegram.storage import SQLiteStorage
from tests.test_auto_replies import setup as setup_fixture
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher

setup = setup_fixture


@pytest.mark.parametrize("enabled", [True, False])
async def test_dispatcher_edit_one_reply_field_preserves_intent_and_limits(setup, enabled):
    store, api, _, service = setup
    ident = service.save(
        7,
        {
            "trigger_text": "hello",
            "response_text": "old",
            "enabled": enabled,
            "cooldown": 900,
            "daily_limit": 7,
        },
        armed=enabled,
    )
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        await ui.click("ar:edit:" + ident)
        await ui.click("ar:field:" + ident + ":response_text")
        await ui.message("new answer")
        rule = service.get(7, ident)
        assert rule["response_text"] == "new answer"
        assert bool(rule["enabled"]) is enabled
        assert (rule["trigger_text"], rule["cooldown"], rule["daily_limit"]) == ("hello", 900, 7)
        assert not api.account.sent and not store.rows("SELECT * FROM outbox")
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_dispatcher_stop_during_field_edit_cannot_rearm_rule(setup):
    store, api, _, service = setup
    ident = service.save(7, {"trigger_text": "hello", "response_text": "old", "enabled": True}, armed=True)
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        await ui.click("ar:edit:" + ident)
        await ui.click("ar:field:" + ident + ":response_text")
        service.stop(7)
        await ui.message("must not apply")
        rule = service.get(7, ident)
        assert not rule["enabled"] and rule["response_text"] == "old"
        assert not api.account.sent
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_dispatcher_create_reply_with_two_inputs_stays_disabled(setup):
    store, api, _, service = setup
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        await ui.click("ar:new")
        await ui.message("hello")
        await ui.message("answer")
        rule = service.rules(7)[0]
        assert not rule["enabled"] and rule["cooldown"] == 600 and rule["daily_limit"] == 20
        state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
        assert await state.get_state() is None
        assert not api.account.sent and not store.rows("SELECT * FROM outbox")
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_dispatcher_contains_is_explicit_and_worker_keeps_safe_delivery_guards(setup):
    from app.workers.auto_replies import AutoReplyWorker
    from app.workers.outbox import Executor, OutboxWorker
    from tests.test_auto_replies import incoming

    store, api, messages, service = setup
    ident = service.save(7, {"trigger_text": "hello", "response_text": "answer", "enabled": True}, armed=True)
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        incoming(store, messages, "exact-only", text="hello friend")
        await AutoReplyWorker(store, api).once()
        assert not store.rows("SELECT * FROM outbox")
        await ui.click("ar:edit:" + ident)
        await ui.click("ar:match:" + ident + ":contains")
        incoming(store, messages, "contains", text="HELLO friend")
        await AutoReplyWorker(store, api).once()
        worker = OutboxWorker(store, Executor(store, api, None))
        assert await worker.once()
        assert api.account.sent == [{"chat_id": "chat", "text": "answer"}]
        await ui.click("ar:show:" + ident)
        assert "Содержит фразу" in bot.session.calls[-1].text
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_delayed_matching_message_is_explained_but_never_dispatched(setup):
    from datetime import UTC, datetime, timedelta

    from app.workers.auto_replies import AutoReplyWorker
    from tests.test_auto_replies import incoming

    store, api, messages, service = setup
    ident = service.save(7, {"trigger_text": "hello", "response_text": "answer", "enabled": True}, armed=True)
    old = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    store.execute(
        "UPDATE auto_reply_rules SET updated_at=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(minutes=10)).isoformat(), ident),
    )
    incoming(store, messages, text="hello", created_at=old)
    await AutoReplyWorker(store, api).once()
    assert store.rows("SELECT error_code FROM auto_reply_events")[0]["error_code"] == "SYNC_DELAY"
    assert not api.account.sent and not store.rows("SELECT * FROM outbox")
