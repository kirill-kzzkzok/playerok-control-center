import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, Store
from app.services.orders import Orders
from app.services.replies import Replies, reply_text
from app.telegram.replies_panel import RepliesPanel
from app.telegram.storage import SQLiteStorage
from app.workers.notifications import Notifications


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "replies.sqlite")
    store.claim_first_owner(42)
    store.set_setting("notifications:initialized", True)
    yield store
    store.close()


def origin(store):
    Orders(store).ingest_message(
        "chat", {"id": "incoming", "text": "<question>", "user": {"username": "buyer"}}
    )
    store.execute("INSERT INTO notifications VALUES('message:incoming',42,99,'SUCCEEDED')")
    return "message:incoming"


def pending(event=None):
    return {"kind": "reply", "target": "chat", "payload": {"text": "Private response"}, "origin_event": event}


def finish(store, status="SUCCEEDED"):
    action = store.claim()
    store.finish(action["id"], action["lease_token"], status, result={"message_id": "remote-message"})


def test_templates_owner_isolation_revision_and_limit(store):
    service = Replies(store)
    ident = service.save(42, "Greeting", "Hello <buyer>")
    assert not service.list(43)
    with pytest.raises(Conflict):
        service.get(43, ident)
    service.save(42, "Greeting", "Updated", ident=ident, revision=0)
    with pytest.raises(Conflict):
        service.save(42, "Greeting", "Stale", ident=ident, revision=0)
    with pytest.raises(Conflict):
        service.delete(42, ident, 0)
    for index in range(19):
        service.save(42, str(index), "answer")
    with pytest.raises(Conflict, match="20"):
        service.save(42, "overflow", "answer")
    service.delete(42, ident, 1)
    assert len(service.list(42)) == 19


@pytest.mark.parametrize("text", [" ", "😀" * 1751, "x" * 3501, None])
def test_reply_validation_matches_telegram_utf16_limit(text):
    with pytest.raises(ValueError):
        reply_text(text)


def test_queue_links_only_verified_owner_chat_and_rolls_back_invalid_link(store):
    event = origin(store)
    service = Replies(store)
    assert service.origin(42, 99, "chat") == event
    assert service.origin(43, 99, "chat") is None
    assert service.origin(42, 99, "other") is None
    with pytest.raises(Conflict):
        service.enqueue(43, "foreign", pending(event))
    assert not store.rows("SELECT * FROM outbox")
    ident = service.enqueue(42, "reply", pending(event))
    assert service.enqueue(42, "reply", pending(event)) == ident
    assert len(store.rows("SELECT * FROM notification_actions")) == 1
    assert "origin_event" not in json.loads(store.rows("SELECT payload FROM outbox")[0]["payload"])


async def test_notification_edit_after_reply_survives_restart_without_buyer_resend(store):
    service = Replies(store)
    service.enqueue(42, "reply", pending(origin(store)))
    finish(store)
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(side_effect=[TimeoutError, True]), send_message=AsyncMock()
    )
    await Notifications(store, bot).once()
    assert store.rows("SELECT rendered_status FROM notification_actions")[0]["rendered_status"] is None
    await Notifications(store, bot).once()
    assert bot.edit_message_text.await_count == 1  # persisted backoff, no hot retry loop
    store.execute("UPDATE notification_actions SET retry_after=0")
    await Notifications(store, bot).once()
    await Notifications(store, bot).once()
    assert bot.edit_message_text.await_count == 2
    assert bot.edit_message_text.call_args.kwargs["message_id"] == 99
    assert "&lt;question&gt;" in bot.edit_message_text.call_args.args[0]
    assert "remote-message" in bot.edit_message_text.call_args.args[0]
    assert "Private response" not in bot.edit_message_text.call_args.args[0]
    bot.send_message.assert_not_called()
    assert len(store.rows("SELECT * FROM outbox")) == 1


async def test_uncertain_reply_is_visible_and_never_labelled_sent(store):
    Replies(store).enqueue(42, "reply", pending(origin(store)))
    finish(store, "UNCERTAIN")
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(), send_message=AsyncMock(return_value=SimpleNamespace(message_id=70))
    )
    await Notifications(store, bot).once()
    text = bot.edit_message_text.call_args.args[0]
    assert "результат неизвестен" in text
    assert "подтверждено API" not in text


async def test_quick_reply_preview_is_complete_and_requires_separate_commit(store):
    ident = Replies(store).save(42, "Hello", "<&>" * 1000)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=42, user_id=42))
    panel = RepliesPanel(store)
    _, markup = await panel.menu(42, state, chat_id="chat", origin=origin(store))
    await panel.callback(markup.inline_keyboard[0][0].callback_data, 42, state)
    view = (await state.get_data())["quick_view"]
    # Reconstruct state/panel to exercise durable restoration.
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=42, user_id=42))
    text, markup = await RepliesPanel(store).callback(f"quick:{view['nonce']}:use:{ident}", 42, state)
    assert text.count("&lt;&amp;&gt;") == 1000
    assert markup.inline_keyboard[0][0].callback_data.startswith("commit:")
    assert (await state.get_data())["pending"]["payload"]["text"] == "<&>" * 1000
    assert not store.rows("SELECT * FROM outbox")
    with pytest.raises(ValueError, match="устарела"):
        await panel.callback(f"quick:{view['nonce']}:use:{ident}", 42, state)


async def test_quick_reply_new_template_survives_restart(store):
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=42, user_id=42))
    panel = RepliesPanel(store)
    _, markup = await panel.menu(42, state)
    await panel.callback(markup.inline_keyboard[0][0].callback_data, 42, state)
    await panel.message(SimpleNamespace(text="My template", from_user=SimpleNamespace(id=42)), state)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=42, user_id=42))
    await RepliesPanel(store).message(
        SimpleNamespace(text="My text", from_user=SimpleNamespace(id=42)), state
    )
    assert Replies(store).list(42)[0]["text"] == "My text"


async def test_manual_confirmation_is_labelled_owner_decision_and_obsolete_alert_is_skipped(store):
    from app.database.store import now
    from app.services.reconciliation import Reconciliation, signature

    ident = Replies(store).enqueue(42, "reply", pending(origin(store)))
    finish(store, "UNCERTAIN")
    api = SimpleNamespace(
        account=SimpleNamespace(id="seller"),
        call=AsyncMock(
            return_value={
                "messages": [
                    {"id": "sent", "user": {"id": "seller"}, "text": "Private response", "created_at": now()}
                ],
                "page_info": {"has_next_page": False},
            }
        ),
    )
    await Reconciliation(store, api).resolve(
        42,
        ident,
        signature(store.rows("SELECT * FROM outbox")[0]),
        "CONFIRM_MESSAGE",
        "Лично сверил исходящее сообщение",
        "sent",
    )
    bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
    await Notifications(store, bot).once()
    assert "подтверждено владельцем" in bot.edit_message_text.call_args.args[0]
    assert "подтверждено API" not in bot.edit_message_text.call_args.args[0]
    bot.send_message.assert_not_called()
