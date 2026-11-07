from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.store import Store
from app.services.orders import Orders
from app.workers.notifications import Notifications


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "notify.sqlite")
    value.claim_first_owner(42)
    value.set_setting("sync:health", {"status": "OK"})
    yield value
    value.close()


async def test_historical_baseline_and_new_message_sent_once(store):
    orders = Orders(store)
    orders.ingest_message("chat1", {"id": "old", "text": "history"})
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=9)))
    worker = Notifications(store, bot)
    await worker.once()
    bot.send_message.assert_not_called()
    orders.ingest_message("chat1", {"id": "new", "text": "<new>"})
    await worker.once()
    await worker.once()
    assert bot.send_message.await_count == 1
    assert "&lt;new&gt;" in bot.send_message.call_args.args[1]
    assert (
        store.rows("SELECT status FROM notifications WHERE event_key='message:new'")[0]["status"]
        == "SUCCEEDED"
    )


async def test_telegram_failure_is_uncertain_without_duplicate(store):
    store.set_setting("notifications:initialized", True)
    Orders(store).ingest_message("chat1", {"id": "new", "text": "message"})
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError))
    worker = Notifications(store, bot)
    await worker.once()
    await worker.once()
    assert bot.send_message.await_count == 1
    assert store.rows("SELECT status FROM notifications")[0]["status"] == "UNCERTAIN"


async def test_no_owner_does_not_mark_notification_baseline(store):
    store.execute("DELETE FROM administrators")
    bot = SimpleNamespace(send_message=AsyncMock())
    await Notifications(store, bot).once()
    assert not store.setting("notifications:initialized", False)
    bot.send_message.assert_not_called()


async def test_self_messages_do_not_starve_notifications_behind_batch_limit(store):
    store.set_setting("notifications:initialized", True)
    store.set_setting("playerok:account_id", "seller")
    orders = Orders(store)
    for index in range(201):
        orders.ingest_message("chat", {"id": f"self-{index}", "user": {"id": "seller"}})
    orders.ingest_message("chat", {"id": "buyer", "text": "hello", "user": {"id": "buyer"}})
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)))
    worker = Notifications(store, bot)
    await worker.once()
    await worker.once()
    await worker.once()
    assert bot.send_message.await_count == 1
    assert len(store.rows("SELECT * FROM notifications WHERE status='SKIPPED'")) == 201


async def test_uncertain_operation_alert_does_not_wait_for_initial_sync_or_repeat(store):
    store.set_setting("sync:health", {"status": "ERROR"})
    Orders(store).ingest_message("chat", {"id": "history", "text": "old"})
    ident = store.enqueue("reply", "reply", "chat", {"text": "private response"})
    action = store.claim()
    store.finish(ident, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)))
    worker = Notifications(store, bot)
    await worker.once()
    await worker.once()
    assert bot.send_message.await_count == 1
    text = bot.send_message.call_args.args[1]
    assert "Неопределённый" in text and ident not in text
    assert any(
        button.callback_data == "ops:show:" + ident
        for row in bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    )
    assert "private response" not in text and "old" not in text
    assert not store.setting("notifications:initialized", False)
    store.set_setting("sync:health", {"status": "OK"})
    await worker.once()
    assert bot.send_message.await_count == 1


async def test_recovered_expired_action_emits_one_persistent_alert(store):
    ident = store.enqueue("lost", "reply", "chat", {"text": "private response"})
    action = store.claim()
    store.mark_started(action)
    store.execute("UPDATE outbox SET lease_until=0")
    assert store.recover() == 1
    assert store.recover() == 0
    events = store.rows("SELECT payload FROM inbox WHERE kind='operation_uncertain'")
    assert len(events) == 1
    assert ident in events[0]["payload"]
    assert "private response" not in events[0]["payload"]
