"""New sales/messages cannot become historical because an old chat is unavailable."""

from copy import deepcopy

from app.database.store import now
from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.workers.notifications import Notifications
from app.workers.sync import Synchronizer
from tests.test_unified_delivery_controls import shop as shop_fixture

shop = shop_fixture


async def test_new_sale_and_message_survive_old_chat_backfill_and_later_recovery(shop):
    r = await shop()
    recovered = False
    current = []
    old = {
        "id": "historical",
        "text": "Старая переписка",
        "created_at": "2020-01-01T00:00:00+00:00",
        "user": {"id": "buyer"},
    }
    history = r.api.order("history", status="CONFIRMED")
    history["created_at"] = "2020-01-01T00:00:00+00:00"
    pending = r.api.order("pending-before-start", status="PENDING")
    pending["created_at"] = "2020-01-01T00:00:00+00:00"
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_chats":
            return r.api.page("chats", [{"id": "old"}, {"id": "new-chat", "user": {"username": "customer"}}])
        if method == "get_chat_messages":
            if kwargs["chat_id"] == "old":
                if not recovered:
                    raise APIError("HTTP_503")
                return r.api.page("messages", [old])
            return r.api.page("messages", current)
        return await original(method, **kwargs)

    r.api.call = call
    sync = Synchronizer(r.store, r.api, Orders(r.store))
    await sync.once()
    assert r.store.setting("sync:health")["status"] == "ERROR"
    r.api.order("new-paid")
    pending["status"] = "PAID"
    current.append(
        {"id": "new-message", "text": "Новый вопрос покупателя", "created_at": now(), "user": {"id": "buyer"}}
    )
    await sync.once()
    notifications = Notifications(r.store, r.bot)
    await notifications.once()
    statuses = {row["event_key"]: row["status"] for row in r.store.rows("SELECT * FROM notifications")}
    assert statuses["order:new-paid:PAID"] == "SUCCEEDED"
    assert statuses["message:new-message"] == "SUCCEEDED"
    assert statuses["order:history:CONFIRMED"] == "BASELINE"
    assert statuses["order:pending-before-start:PAID"] == "SUCCEEDED"
    assert r.store.rows("SELECT telegram_message_id FROM chat_cards WHERE chat_id='new-chat'")[0][
        "telegram_message_id"
    ]
    # Restart reconstructs both workers, without replacing the durable boundary.
    saved = deepcopy(r.store.setting("notifications:baseline"))
    sync = Synchronizer(r.store, r.api, Orders(r.store))
    recovered = True
    await sync.once()
    await Notifications(r.store, r.bot).once()
    assert r.store.setting("notifications:baseline") == saved
    statuses = {row["event_key"]: row["status"] for row in r.store.rows("SELECT * FROM notifications")}
    assert statuses["message:historical"] == "BASELINE"
    assert statuses["message:new-message"] == "SUCCEEDED"
    assert statuses["order:new-paid:PAID"] == "SUCCEEDED"
    assert not r.api.messages and not r.api.remote.payments
