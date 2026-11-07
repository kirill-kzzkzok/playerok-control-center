"""Real Notifications/Dispatcher/Outbox; fake transports only at the API edges."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram import Bot

from app.database.store import Store
from app.services.orders import Orders
from app.services.products import Products
from app.workers.notifications import Notifications
from app.workers.outbox import Executor, OutboxWorker
from tests.stateful_telegram import StatefulTelegram
from tests.test_logical_products import ReadAPI
from tests.test_telegram_integration import Interaction
from tests.test_telegram_transport import production_dispatcher


@pytest.fixture
async def cards(tmp_path):
    store = Store(tmp_path / "cards.sqlite")
    store.claim_first_owner(7)
    store.set_setting("playerok:account_id", "seller")
    store.set_setting("notifications:initialized", True)
    api = ReadAPI()
    sent = []

    async def call(method, **kwargs):
        if method == "get_chat":
            return {"id": kwargs["chat_id"], "is_texting_allowed": True}
        raise AssertionError(method)

    def send_message(chat_id, text):
        sent.append((chat_id, text))
        return {"id": "server-" + str(len(sent))}

    api.call = call
    api.account.send_message = send_message
    session = StatefulTelegram()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, production_dispatcher(store, api))
    r = SimpleNamespace(
        store=store,
        api=api,
        bot=bot,
        session=session,
        ui=ui,
        sent=sent,
        notifications=Notifications(store, bot),
        worker=OutboxWorker(store, Executor(store, api, Products(store, api))),
    )
    yield r
    await session.close()
    store.close()


def incoming(r, ident, chat="chat", text=None):
    value = {
        "id": str(ident),
        "text": text or "message " + str(ident),
        "user": {"id": "buyer", "username": "buyer"},
        "created_at": (
            datetime.now(UTC) + timedelta(microseconds=int(ident) if str(ident).isdigit() else 0)
        ).isoformat(),
    }
    Orders(r.store).ingest_message(chat, deepcopy(value))
    return value


def calls(r, method):
    return [m for m in r.session.calls if m.__api_method__ == method]


def test_buyer_console_logs_playerok_nick_once_without_message_text(cards, caplog):
    caplog.set_level("INFO", logger="pcc.shop")
    message = {
        "id": "buyer-message",
        "text": "private customer content",
        "user": {"id": "buyer", "username": "Buyer_42"},
    }
    orders = Orders(cards.store)
    assert orders.ingest_message("chat", message)
    assert not orders.ingest_message("chat", message)
    assert caplog.messages.count("📩 Новое сообщение от покупателя Buyer_42") == 1
    assert "private customer content" not in caplog.text


async def test_ten_incoming_events_update_one_card_and_keep_two_buttons(cards):
    r = cards
    for n in range(10):
        incoming(r, n)
        await r.notifications.once()
    assert len(calls(r, "sendMessage")) == 1
    assert len(calls(r, "editMessageText")) == 9
    row = r.store.rows("SELECT * FROM chat_cards")[0]
    text = r.session.messages[row["telegram_message_id"]]["text"]
    assert "message 0" in text and "message 9" in text
    assert len(r.store.rows("SELECT * FROM chat_messages")) == 10
    buttons = r.session.messages[row["telegram_message_id"]]["reply_markup"].inline_keyboard
    assert [b.text for group in buttons for b in group] == ["✉️ Ответить", "✖️ Закрыть"]
    assert not r.sent


async def test_concurrent_batch_dedup_two_chats_close_reopen_and_restart(cards):
    from app.telegram.chat_cards import ChatCards

    r = cards
    value = incoming(r, 1)
    Orders(r.store).ingest_message("chat", value)
    incoming(r, 2, chat="other")
    await asyncio.gather(r.notifications.once(), Notifications(r.store, r.bot).once())
    assert len(calls(r, "sendMessage")) == 2
    rows = r.store.rows("SELECT * FROM chat_cards ORDER BY chat_id")
    row = rows[0]
    await r.ui.click("cc:close:" + row["token"], message_id=row["telegram_message_id"])
    await r.ui.click("cc:close:" + row["token"], message_id=row["telegram_message_id"])
    assert (
        r.store.rows("SELECT state FROM chat_cards WHERE chat_id=?", (row["chat_id"],))[0]["state"]
        == "CLOSED"
    )
    assert len(r.store.rows("SELECT * FROM chat_messages")) == 2
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind='mark_read'")
    r.ui.dp = production_dispatcher(r.store, r.api)
    incoming(r, 3, chat=row["chat_id"])
    await Notifications(r.store, r.bot).once()
    new = r.store.rows("SELECT * FROM chat_cards WHERE chat_id=?", (row["chat_id"],))[0]
    assert new["state"] == "OPEN" and new["token"] != row["token"]
    assert new["telegram_message_id"] != row["telegram_message_id"]
    assert len(calls(r, "sendMessage")) == 3
    await ChatCards(r.store, r.bot).flush()
    assert len(calls(r, "sendMessage")) == 3


async def test_dispatcher_reply_no_confirmation_and_receipt_updates_same_card(cards):
    r = cards
    incoming(r, 1)
    await r.notifications.once()
    row = r.store.rows("SELECT * FROM chat_cards")[0]
    await r.ui.click("cc:reply:" + row["token"], message_id=row["telegram_message_id"])
    assert not r.sent
    await r.ui.message("Ответ именно этому покупателю")
    assert len(r.store.rows("SELECT * FROM outbox WHERE kind='reply'")) == 1
    assert not r.sent
    await r.worker.once()
    await r.notifications.once()
    assert r.sent == [("chat", "Ответ именно этому покупателю")]
    assert len(calls(r, "sendMessage")) == 1
    assert "Ответ именно этому покупателю" in r.session.messages[row["telegram_message_id"]]["text"]
    assert "Вы" in r.session.messages[row["telegram_message_id"]]["text"]


@pytest.mark.parametrize("lost", [False, True])
async def test_edit_timeout_and_deleted_card_recovery_do_not_resend(cards, lost):
    r = cards
    incoming(r, 1)
    await r.notifications.once()
    row = r.store.rows("SELECT * FROM chat_cards")[0]
    if lost:
        r.session.inject("editMessageText", "after")
    else:
        r.session.messages[row["telegram_message_id"]]["exists"] = False
    incoming(r, 2)
    await r.notifications.once()
    r.store.execute("UPDATE chat_cards SET retry_after=0")
    await r.notifications.once()
    await r.notifications.once()
    assert len(calls(r, "sendMessage")) == (1 if lost else 2)
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "OPEN"}]


async def test_unknown_telegram_send_never_repeated_but_real_callback_recovers(cards):
    r = cards
    r.session.inject("sendMessage", "after")
    incoming(r, 1)
    await r.notifications.once()
    await r.notifications.once()
    row = r.store.rows("SELECT * FROM chat_cards")[0]
    assert row["state"] == "UNCERTAIN"
    assert len(calls(r, "sendMessage")) == 1
    await r.ui.click("cc:reply:" + row["token"], message_id=101)
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "OPEN"}]
    assert not r.sent


async def test_lost_playerok_reply_is_not_shown_successful_and_blocks_second_send(cards):
    r = cards
    incoming(r, 1)
    await r.notifications.once()
    row = r.store.rows("SELECT * FROM chat_cards")[0]

    def lost(chat_id, text):
        r.sent.append((chat_id, text))
        raise TimeoutError("lost response")

    r.api.account.send_message = lost
    await r.ui.click("cc:reply:" + row["token"], message_id=row["telegram_message_id"])
    await r.ui.message("First")
    await r.worker.once()
    await r.notifications.once()
    assert r.store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
    assert not r.store.rows("SELECT * FROM chat_messages WHERE json_extract(payload,'$.user.id')='seller'")
    await r.ui.click("cc:reply:" + row["token"], message_id=row["telegram_message_id"])
    await r.ui.message("First")
    await r.worker.once()
    assert len(r.sent) == 1
    assert (
        "неизвестен" in r.session.messages[row["telegram_message_id"]]["text"]
        or "не подтверждён" in r.session.messages[row["telegram_message_id"]]["text"]
    )


async def test_long_history_and_cancel_are_read_only(cards):
    r = cards
    for n in range(12):
        incoming(r, n, text="<&>🐻" * 2500)
    await r.notifications.once()
    row = r.store.rows("SELECT * FROM chat_cards")[0]
    assert len(r.session.messages[row["telegram_message_id"]]["text"].encode("utf-16-le")) // 2 <= 4096
    assert len(r.store.rows("SELECT * FROM chat_messages")) == 12
    await r.ui.click("cc:reply:" + row["token"], message_id=row["telegram_message_id"])
    await r.ui.message("/cancel")
    assert not r.store.rows("SELECT * FROM outbox") and not r.sent


async def test_retry_after_is_respected_and_creates_only_one_card(cards):
    from aiogram.exceptions import TelegramRetryAfter

    r = cards
    original = r.session.make_request
    limited = False

    async def transport(bot, method, timeout=None):
        nonlocal limited
        if method.__api_method__ == "sendMessage" and not limited:
            limited = True
            r.session.calls.append(method)
            raise TelegramRetryAfter(method=method, message="rate limited", retry_after=60)
        return await original(bot, method, timeout)

    r.session.make_request = transport
    incoming(r, 1)
    await r.notifications.once()
    await r.notifications.once()
    assert len(calls(r, "sendMessage")) == 1 and not r.session.messages
    r.store.execute("UPDATE chat_cards SET retry_after=0")
    await r.notifications.once()
    await r.notifications.once()
    assert len(calls(r, "sendMessage")) == 2 and len(r.session.messages) == 1


async def test_mapping_and_history_survive_database_reopen(cards):
    r = cards
    incoming(r, 1)
    await r.notifications.once()
    first = r.store.rows("SELECT * FROM chat_cards")[0]
    path = r.store.path
    r.store.close()
    r.store = Store(path)
    r.notifications = Notifications(r.store, r.bot)
    incoming(r, 2)
    await r.notifications.once()
    after = r.store.rows("SELECT * FROM chat_cards")[0]
    assert first["token"] == after["token"] and first["telegram_message_id"] == after["telegram_message_id"]
    assert len(calls(r, "sendMessage")) == 1
    assert len(r.store.rows("SELECT * FROM chat_messages")) == 2
    r.store.close()


async def test_closed_or_foreign_tokens_and_old_confirmations_cannot_send(cards):
    r = cards
    incoming(r, 1)
    await r.notifications.once()
    row = r.store.rows("SELECT * FROM chat_cards")[0]
    await r.ui.click("cc:reply:" + row["token"], owner=8, message_id=row["telegram_message_id"])
    await r.ui.click("quick:obsolete:send")
    await r.ui.click("commit:obsolete")
    await r.ui.click("cc:close:" + row["token"], message_id=row["telegram_message_id"])
    await r.ui.click("cc:reply:" + row["token"], message_id=row["telegram_message_id"])
    assert not r.sent and not r.store.rows("SELECT * FROM outbox")
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "CLOSED"}]


async def test_reply_and_fsm_consumption_are_one_transaction(cards):
    import sqlite3

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from app.telegram.chat_cards import ChatCards
    from app.telegram.storage import SQLiteStorage

    r = cards
    incoming(r, 1)
    await r.notifications.once()
    row = r.store.rows("SELECT * FROM chat_cards")[0]
    await r.ui.click("cc:reply:" + row["token"], message_id=row["telegram_message_id"])
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    r.store.execute(
        "CREATE TRIGGER abort_reply_consume BEFORE UPDATE ON fsm WHEN OLD.state='chatcard:reply' AND NEW.state IS NULL BEGIN SELECT RAISE(ABORT,'injected commit failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await ChatCards(r.store, r.bot).reply(
            SimpleNamespace(from_user=SimpleNamespace(id=7), message_id=999, text="Must roll back"),
            state,
            r.api,
        )
    assert not r.store.rows("SELECT * FROM outbox")
    assert await state.get_state() == "chatcard:reply"


async def test_opening_another_dialog_cancels_previous_reply_target(cards):
    r = cards
    incoming(r, 1)
    incoming(r, 2, chat="other")
    await r.notifications.once()
    row = r.store.rows("SELECT * FROM chat_cards WHERE chat_id='chat'")[0]
    await r.ui.click("cc:reply:" + row["token"], message_id=row["telegram_message_id"])
    original = r.api.call

    async def boundary(method, **kwargs):
        if method == "get_chat_messages":
            return {"messages": [], "page_info": {"has_next_page": False}}
        return await original(method, **kwargs)

    r.api.call = boundary
    await r.ui.click("chat:other")
    await r.ui.message("Not a reply to the old buyer")
    assert not r.store.rows("SELECT * FROM outbox")
    assert not r.sent


async def test_more_than_one_batch_of_dialogs_does_not_starve_later_card_updates(cards):
    r = cards
    for n in range(101):
        incoming(r, n, chat="chat-" + str(n))
    # The two-second budget is per cycle, so the number of cycles depends on
    # SQLite latency under parallel test load. Verify progress and no duplicate
    # sends without assuming a throughput that the worker does not promise.
    for _ in range(101):
        sent_before = len(calls(r, "sendMessage"))
        await r.notifications.once()
        sent = len(calls(r, "sendMessage"))
        assert sent_before < sent <= 101
        if sent == 101:
            break
    assert len(calls(r, "sendMessage")) == 101
    incoming(r, 200, chat="chat-100", text="Latest message must appear")
    row = r.store.rows("SELECT * FROM chat_cards WHERE chat_id='chat-100'")[0]
    for _ in range(101):
        await r.notifications.once()
        if "Latest message must appear" in r.session.messages[row["telegram_message_id"]]["text"]:
            break
    assert "Latest message must appear" in r.session.messages[row["telegram_message_id"]]["text"]
    assert len(calls(r, "sendMessage")) == 101


async def test_new_incoming_after_navigation_preserves_form_and_reopens_one_notice(cards):
    r = cards
    incoming(r, 1)
    await r.notifications.once()
    old = r.store.rows("SELECT * FROM chat_cards")[0]
    message_id = old["telegram_message_id"]
    await r.ui.click(
        "ops:list:ALL:0",
        message_id=message_id,
        reply_markup=r.session.messages[message_id]["reply_markup"],
    )
    screen = deepcopy(r.session.messages[message_id])
    assert "История операций" in screen["text"]
    assert r.store.rows("SELECT state FROM chat_cards") == [{"state": "CLOSED"}]
    incoming(r, 2)
    await r.notifications.once()
    current = r.store.rows("SELECT * FROM chat_cards")[0]
    assert current["state"] == "OPEN" and current["telegram_message_id"] != message_id
    assert r.session.messages[message_id] == screen
    assert len(calls(r, "sendMessage")) == 2, [call.text for call in calls(r, "sendMessage")]
    assert len(r.store.rows("SELECT * FROM chat_messages")) == 2
    assert not r.store.rows("SELECT * FROM outbox") and not r.sent
