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
