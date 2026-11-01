"""Independent adversarial evidence for dispatch-phase migration/recovery."""

import pytest

from app.database.store import Store
from app.services.products import Products
from app.workers.outbox import Executor, OutboxWorker
from tests.test_stock_auto_cycle import Marketplace


@pytest.mark.parametrize("old_status", ["RETRYABLE_FAILED", "PENDING"])
async def test_migrated_retryable_unknown_dispatch_is_never_replayed(tmp_path, old_status):
    path = tmp_path / "legacy.sqlite"
    store = Store(path)
    store.claim_first_owner(1)
    ident = store.enqueue("old-reply", "reply", "chat", {"text": "old already attempted reply"}, actor=1)
    store.execute("UPDATE outbox SET status=?,attempts=1 WHERE id=?", (old_status, ident))
    # Materialize the real pre-026 schema on a temporary copy: no dispatch proof
    # existed. Applying migration must conservatively block this unknown attempt.
    store.execute("ALTER TABLE outbox DROP COLUMN started_at")
    store.execute("ALTER TABLE outbox DROP COLUMN retry_at")
    store.execute("DELETE FROM schema_migrations WHERE version=26")
    store.close()
    store = Store(path)
    api = Marketplace("success")
    try:
        assert store.rows("SELECT started_at FROM outbox")[0]["started_at"] is not None
        await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
        assert not api.mutations
        assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
    finally:
        store.close()


async def test_cancellation_during_read_preflight_does_not_quarantine_unexposed_secret(tmp_path):
    import asyncio

    from app.services.orders import Orders

    store = Store(tmp_path / "cancel.sqlite")
    store.claim_first_owner(1)
    api = Marketplace("success")
    api.paid_order()
    Orders(store).ingest(api.orders["order"])
    Orders(store).add_stock("item", ["unexposed-secret"])
    Orders(store).reserve("order", actor=1)
    original = api.call
    started = asyncio.Event()

    async def waiting(method, **kwargs):
        if method == "get_deal":
            started.set()
            await asyncio.Event().wait()
        return await original(method, **kwargs)

    api.call = waiting
    worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
    task = asyncio.create_task(worker.once())
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not api.mutations
        assert store.rows("SELECT started_at FROM outbox") == [{"started_at": None}]
        assert store.rows("SELECT status FROM inventory") == [{"status": "RESERVED"}]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


async def test_late_older_reply_does_not_restore_completed_mark_read_button(tmp_path):
    import asyncio
    from contextlib import nullcontext

    from aiogram import Bot

    from app.services.orders import Orders
    from app.services.replies import Replies
    from app.workers.notifications import Notifications
    from tests.stateful_telegram import StatefulTelegram

    store = Store(tmp_path / "notification-actions.sqlite")
    store.claim_first_owner(1)
    store.set_setting("notifications:initialized", True)
    api = Marketplace("success")
    api.mark_chat_as_read = lambda chat_id: {"id": chat_id, "unread_messages_counter": 0}
    api.account = api
    api.permit = lambda operation: nullcontext()
    api.lock = asyncio.Lock()
    session = StatefulTelegram()
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        Orders(store).ingest_message(
            "chat", {"id": "m", "text": "Question", "user": {"id": "buyer", "username": "Buyer"}}
        )
        notifications = Notifications(store, bot)
        await notifications.once()
        ident = store.rows("SELECT telegram_message_id FROM notifications")[0]["telegram_message_id"]
        replies = Replies(store)
        old = replies.enqueue(
            1,
            "old-reply",
            {
                "kind": "reply",
                "target": "chat",
                "payload": {"text": "An answer"},
                "origin_event": "message:m",
            },
        )
        newer = replies.enqueue(
            1,
            "newer-read",
            {"kind": "mark_read", "target": "chat", "payload": {}, "origin_event": "message:m"},
        )
        worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
        await worker.once(operation_id=newer)
        await notifications.refresh_actions()
        assert not any(
            (b.callback_data or "").startswith("readchat:")
            for row in session.messages[ident]["reply_markup"].inline_keyboard
            for b in row
        )
        await worker.once(operation_id=old)
        await notifications.refresh_actions()
        assert not any(
            (b.callback_data or "").startswith("readchat:")
            for row in session.messages[ident]["reply_markup"].inline_keyboard
            for b in row
        )
    finally:
        await session.close()
        store.close()


async def test_legacy_unknown_delivery_has_consistent_stock_for_positive_receipt_reconciliation(tmp_path):
    from copy import deepcopy

    from app.database.store import now
    from app.services.orders import Orders
    from app.services.reconciliation import Reconciliation, signature

    path = tmp_path / "legacy-delivery.sqlite"
    store = Store(path)
    store.claim_first_owner(1)
    api = Marketplace("success")
    api.paid_order()
    Orders(store).ingest(api.orders["order"])
    Orders(store).add_stock("item", ["isolated-legacy-unit"])
    Orders(store).reserve("order", actor=1)
    ident = store.rows("SELECT id FROM outbox")[0]["id"]
    api.messages.append(
        {
            "id": "legacy-receipt",
            "text": "isolated-legacy-unit",
            "user": {"id": "seller"},
            "created_at": now(),
        }
    )
    store.execute("UPDATE outbox SET status='RETRYABLE_FAILED',attempts=1 WHERE id=?", (ident,))
    store.execute("ALTER TABLE outbox DROP COLUMN started_at")
    store.execute("ALTER TABLE outbox DROP COLUMN retry_at")
    store.execute("DELETE FROM schema_migrations WHERE version=26")
    store.close()
    store = Store(path)
    try:
        service = Reconciliation(store, api)
        action = service.action(1, ident, uncertain=True)
        assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
        await service.resolve(
            1,
            ident,
            signature(deepcopy(action)),
            "CONFIRM_MESSAGE",
            "Проверил точную квитанцию сообщения",
            "legacy-receipt",
        )
        assert store.rows("SELECT status FROM inventory") == [{"status": "DELIVERED"}]
        assert not api.mutations
    finally:
        store.close()
