import asyncio
from types import SimpleNamespace

import pytest

from app.database.store import Store
from app.telegram.chat_cards import ChatCards
from app.telegram.errors import TelegramRequestNotStarted
from app.workers.notifications import Notifications
from tests.test_chat_cards import calls, incoming
from tests.test_chat_cards import cards as cards_fixture

cards = cards_fixture


async def test_happ_predispatch_outage_recovers_one_card_with_all_messages(cards, monkeypatch):
    r = cards
    clock = [1000.0]
    monkeypatch.setattr(
        "app.telegram.chat_cards.time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])
    )
    original = r.session.make_request
    online = False

    async def transport(bot, method, timeout=None):
        if method.__api_method__ == "sendMessage" and not online:
            raise TelegramRequestNotStarted(method=method, message="Happ unavailable")
        return await original(bot, method, timeout)

    r.session.make_request = transport
    incoming(r, 1)
    await r.notifications.once()
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "NEW"}]
    for n in range(2, 5):
        incoming(r, n)
    # Safe readmission failures must not hit a permanent eight-attempt ceiling.
    for _ in range(12):
        clock[0] += 60
        await r.notifications.once()
    online = True
    clock[0] += 60
    await r.notifications.once()
    await r.notifications.once()
    assert len(calls(r, "sendMessage")) == 1
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "OPEN"}]
    assert len(r.store.rows("SELECT * FROM chat_messages")) == 4
    assert r.store.rows("SELECT DISTINCT status FROM notifications") == [{"status": "SUCCEEDED"}]


async def test_stale_card_claim_cannot_send_a_second_notification(cards):
    r = cards
    incoming(r, 1)
    service = ChatCards(r.store, r.bot)
    service.touch(7, "chat")
    stale = r.store.rows("SELECT * FROM chat_cards")[0]
    await service.update_card(stale)
    await service.update_card(stale)
    assert len(calls(r, "sendMessage")) == 1
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "OPEN"}]


async def test_five_second_send_outlives_background_budget_and_is_not_duplicated(cards):
    r = cards
    entered = asyncio.Event()
    original = r.session.make_request

    async def transport(bot, method, timeout=None):
        if method.__api_method__ == "sendMessage":
            entered.set()
            await asyncio.sleep(5)
        return await original(bot, method, timeout)

    r.session.make_request = transport
    incoming(r, 1)
    await r.notifications.once()
    assert entered.is_set()
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "SENDING"}]
    for n in range(2, 5):
        incoming(r, n)
    await r.notifications.once()
    await asyncio.sleep(1.3)
    await r.notifications.once()
    assert len(calls(r, "sendMessage")) == 1
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "OPEN"}]
    assert "message 4" in str(r.session.messages)
    assert len(r.store.rows("SELECT * FROM chat_messages")) == 4


async def test_restart_with_started_send_keeps_unknown_and_messages_visible(cards):
    r = cards
    incoming(r, 1)
    cards_service = ChatCards(r.store, r.bot)
    cards_service.touch(7, "chat")
    r.store.execute("UPDATE chat_cards SET state='SENDING',busy_until=0,busy_token='old-process'")
    restored = Store(r.store.path)
    try:
        await Notifications(restored, r.bot).once()
        assert restored.rows("SELECT state FROM chat_cards") == [{"state": "UNCERTAIN"}]
        assert not calls(r, "sendMessage")
        assert restored.rows("SELECT COUNT(*) AS n FROM chat_messages")[0]["n"] == 1
        original = r.api.call

        async def read(method, **kwargs):
            if method == "get_chats":
                return {"chats": [{"id": "chat"}], "page_info": {"has_next_page": False}}
            return await original(method, **kwargs)

        r.api.call = read
        await r.ui.click("chats")
        assert "chat" in str(r.session.last_panel.reply_markup)
    finally:
        restored.close()


async def test_cancellation_during_possible_send_is_durably_unknown(cards):
    r = cards
    entered = asyncio.Event()

    async def transport(bot, method, timeout=None):
        entered.set()
        await asyncio.Event().wait()

    r.session.make_request = transport
    incoming(r, 1)
    ChatCards(r.store, r.bot).touch(7, "chat")
    task = asyncio.create_task(ChatCards(r.store, r.bot).flush())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ChatCards(r.store, r.bot).flush()
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "UNCERTAIN"}]


async def test_notification_worker_shutdown_joins_card_task_and_keeps_unknown(cards):
    r = cards
    entered = asyncio.Event()
    stop = asyncio.Event()

    async def transport(bot, method, timeout=None):
        entered.set()
        await asyncio.Event().wait()

    r.session.make_request = transport
    incoming(r, 1)
    task = asyncio.create_task(r.notifications.run(stop))
    await asyncio.wait_for(entered.wait(), 2)
    stop.set()
    await asyncio.wait_for(task, 4)
    assert r.store.chat_card_flush_task is None
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "UNCERTAIN"}]
    assert r.store.rows("SELECT DISTINCT status FROM notifications") == [{"status": "UNCERTAIN"}]


async def test_lost_send_receipt_with_three_later_messages_is_never_resent(cards):
    r = cards
    r.session.inject("sendMessage", "after")
    incoming(r, 1)
    await r.notifications.once()
    for n in range(2, 5):
        incoming(r, n)
    await r.notifications.once()
    await r.notifications.once()
    assert len(calls(r, "sendMessage")) == 1
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "UNCERTAIN"}]
    assert len(r.store.rows("SELECT * FROM chat_messages")) == 4


@pytest.mark.parametrize("phase", ["send", "edit_missing"])
async def test_owner_close_during_send_is_not_undone_by_late_network_error(cards, phase):
    from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

    r = cards
    release = asyncio.Event()
    original = r.session.make_request
    if phase == "edit_missing":
        incoming(r, 0)
        await r.notifications.once()

    async def transport(bot, method, timeout=None):
        result = await original(bot, method, timeout)
        if method.__api_method__ == ("sendMessage" if phase == "send" else "editMessageText"):
            await release.wait()
            if phase == "edit_missing":
                raise TelegramBadRequest(method=method, message="message to edit not found")
            raise TelegramNetworkError(method=method, message="response lost after delivery")
        return result

    r.session.make_request = transport
    incoming(r, 1)
    await r.notifications.once()
    row = r.store.rows("SELECT * FROM chat_cards")[0]
    await r.ui.click("cc:close:" + row["token"], message_id=r.session.sequence)
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "CLOSED"}]
    release.set()
    await r.store.chat_card_flush_task
    await r.notifications.once()
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "CLOSED"}]
    assert len(calls(r, "sendMessage")) == 1
