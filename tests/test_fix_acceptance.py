"""Final evidence for event/backoff/ACK contracts; API boundaries only."""

import asyncio
import json
import time

import pytest
from aiogram import BaseMiddleware

from app.services.orders import Orders
from app.services.replies import Replies
from app.workers.notifications import Notifications
from tests.test_fix_navigation import ui as ui_fixture
from tests.test_fix_observability import case as case_fixture


@pytest.fixture(name="case")
async def _case(tmp_path):
    async for value in case_fixture.__wrapped__(tmp_path):
        yield value


@pytest.fixture(name="ui")
async def _ui(tmp_path):
    async for value in ui_fixture.__wrapped__(tmp_path):
        yield value


async def test_notification_backoff_isolated_from_later_action_and_not_modified_is_success(case, monkeypatch):
    r = case
    r.api.account.mark_chat_as_read = lambda chat_id: {"id": chat_id, "unread_messages_counter": 0}
    Orders(r.store).ingest_message("chat", {"id": "m", "user": {"id": "buyer"}, "text": "Question"})
    notices = Notifications(r.store, r.bot)
    await notices.once()
    replies = Replies(r.store)
    old = replies.enqueue(
        7,
        "old",
        {"kind": "reply", "target": "chat", "payload": {"text": "Answer"}, "origin_event": "message:m"},
    )
    await r.worker.once(operation_id=old)
    r.session.inject("editMessageText", "after")  # Telegram updated known message; ACK lost.
    await notices.refresh_actions()
    before = r.store.rows(
        "SELECT failures,retry_after FROM notification_actions WHERE operation_id=?", (old,)
    )[0]
    newer = replies.enqueue(
        7, "new", {"kind": "mark_read", "target": "chat", "payload": {}, "origin_event": "message:m"}
    )
    await r.worker.once(operation_id=newer)
    await notices.refresh_actions()
    assert (
        r.store.rows("SELECT failures,retry_after FROM notification_actions WHERE operation_id=?", (old,))[0]
        == before
    )
    assert r.store.rows(
        "SELECT rendered_status,failures FROM notification_actions WHERE operation_id=?", (newer,)
    ) == [{"rendered_status": "SUCCEEDED", "failures": 0}]
    monkeypatch.setattr(time, "time", lambda: before["retry_after"] + 1)
    await notices.refresh_actions()  # Strict transport returns "message is not modified".
    assert r.store.rows(
        "SELECT rendered_status,failures FROM notification_actions WHERE operation_id=?", (old,)
    ) == [{"rendered_status": "SUCCEEDED", "failures": 0}]
    assert len(r.api.messages) == 1
    assert not await r.worker.once()


async def test_obsolete_order_event_is_skipped_not_rendered_as_current_status(case):
    r = case
    order = r.api.order("late")
    Orders(r.store).ingest(order)
    Orders(r.store).ingest({**order, "status": "SENT"})
    await Notifications(r.store, r.bot).once()
    assert r.store.rows("SELECT status FROM notifications WHERE event_key='order:late:PAID'") == [
        {"status": "SKIPPED"}
    ]
    assert r.store.rows("SELECT status FROM notifications WHERE event_key='order:late:SENT'") == [
        {"status": "SUCCEEDED"}
    ]
    assert r.store.rows("SELECT status FROM orders WHERE id='late'") == [{"status": "SENT"}]
    notification = r.store.rows(
        "SELECT telegram_message_id FROM notifications WHERE event_key='order:late:SENT'"
    )[0]
    text = r.session.messages[notification["telegram_message_id"]]["text"]
    assert "SENT" not in text and "Продавец отметил отправку" in text


@pytest.mark.parametrize("action", ["items", "commit:old-confirmation"])
async def test_stale_ack_read_survives_mutation_preserves_state(ui, action):
    r = ui
    await r.state.set_data(
        {
            "confirm_token": "old-confirmation",
            "pending": {"kind": "reply", "target": "chat", "payload": {"text": "Answer"}},
        }
    )
    before = await r.state.get_data()
    panel = r.store.setting("navigation:123:7")

    class SimulatedFSMWait(BaseMiddleware):
        async def __call__(self, handler, event, data):
            data["callback_ack"]["status"] = "stale"  # Telegram definitively rejected ACK.
            return await handler(event, data)

    r.ui.dp.update.outer_middleware(SimulatedFSMWait())
    await asyncio.wait_for(r.ui.click(action), 3)
    assert len(r.session.acks) == 1
    assert not r.store.rows("SELECT * FROM outbox")
    if action.startswith("commit:"):
        assert await r.state.get_data() == before
        assert r.store.setting("navigation:123:7") == panel
    else:
        assert r.store.setting("navigation:123:7")["route"] == "items"


async def test_fresh_ack_waiting_for_fsm_does_not_expire_valid_send_confirmation(case):
    r = case

    class QueuedAfterAck(BaseMiddleware):
        async def __call__(self, handler, event, data):
            if "callback_ack" in data:
                data["callback_ack"]["checked_at"] -= 11
            return await handler(event, data)

    r.ui.dp.update.outer_middleware(QueuedAfterAck())
    await r.ui.click("compose:chat")
    await r.ui.message("Confirmed after FSM wait")
    assert len(r.store.rows("SELECT * FROM outbox")) == 1
    await r.worker.once()
    await r.ui.click("commit:obsolete")
    assert not await r.worker.once()
    assert len(r.api.messages) == 1


async def test_owner_recovery_of_proven_preflight_stop_uses_same_operation_and_reserve(case):
    r = case
    r.api.order("stock")
    await r.ui.click("order:stock")
    await r.ui.click("od:stock:stock")
    await r.ui.message("isolated-recovery-unit")

    def token(key):
        return json.loads(r.store.rows("SELECT data FROM fsm")[0]["data"])[key]

    await r.ui.click("stock:commit:" + token("stock_pending")["nonce"])
    await r.ui.click("od:prepare:stock")
    await r.ui.click("od:apply:" + token("order_token"))
    ident = r.store.rows("SELECT id FROM outbox")[0]["id"]
    reserved = r.store.rows("SELECT * FROM reservations")
    await r.ui.click("od:problem:stock")
    await r.ui.message("Hold before sending")
    await r.ui.click("od:apply:" + token("order_token"))
    await r.worker.once()
    assert not r.api.messages
    assert r.store.rows("SELECT status,started_at FROM outbox") == [
        {"status": "PERMANENT_FAILED", "started_at": None}
    ]
    await r.ui.click("od:clear:stock")
    await r.ui.click("od:apply:" + token("order_token"))
    await r.ui.click("ops:show:" + ident)
    await r.ui.click("ops:preparechecks:" + ident)
    nonce = token("retry_checks")["nonce"]
    await r.ui.click("ops:retrychecks:" + nonce)
    await r.ui.click("ops:retrychecks:" + nonce)
    await r.worker.once()
    assert r.store.rows("SELECT * FROM reservations") == reserved
    assert r.store.rows("SELECT id,status FROM outbox") == [{"id": ident, "status": "SUCCEEDED"}]
    assert len(r.api.messages) == 1
    assert not await r.worker.once()
