import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.store import Conflict, Store, now
from app.services.attachments import Attachments
from app.services.orders import Orders
from app.services.reconciliation import Reconciliation, signature
from app.workers.cleanup import UploadCleanup


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "resolve.sqlite")
    store.claim_first_owner(1)
    api = SimpleNamespace(account=SimpleNamespace(id="seller"))
    api.call = AsyncMock(
        return_value={
            "messages": [
                {"id": "sent", "user": {"id": "seller"}, "text": "private code", "created_at": now()}
            ],
            "page_info": {"has_next_page": False},
        }
    )
    yield store, api, Reconciliation(store, api)
    store.close()


def uncertain(store, kind="reply", target="chat", payload=None):
    ident = store.enqueue("action", kind, target, payload or {"text": "private code"})
    action = store.claim()
    store.finish(ident, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
    return ident, signature(store.rows("SELECT * FROM outbox WHERE id=?", (ident,))[0])


async def test_verified_message_resolves_locally_without_sending_and_records_reason(setup):
    store, api, service = setup
    ident, expected = uncertain(store)
    await service.resolve(1, ident, expected, "CONFIRM_MESSAGE", "Проверил сообщение на Playerok", "sent")
    row = store.rows("SELECT status,result,error_code FROM outbox")[0]
    assert row["status"] == "SUCCEEDED" and row["error_code"] is None
    assert json.loads(row["result"])["resolution_source"] == "OWNER_DECISION"
    assert (
        store.rows("SELECT previous_error_code FROM operation_resolutions")[0]["previous_error_code"]
        == "NETWORK_ERROR"
    )
    assert api.call.call_args.args[0] == "get_chat_messages"
    assert store.claim() is None
    assert "private code" not in str(store.rows("SELECT * FROM operation_resolutions"))
    assert "private code" not in str(store.rows("SELECT * FROM audit_log"))
    with pytest.raises(Conflict):
        await service.resolve(1, ident, expected, "CONFIRM_MESSAGE", "Повторное подтверждение", "sent")


@pytest.mark.parametrize(
    "change",
    [
        {"user": {"id": "buyer"}},
        {"text": "different"},
        {"deleted_at": "today"},
        {"created_at": "2020-01-01T00:00:00Z"},
        {"created_at": None},
        {"images": [{"id": "extra"}]},
    ],
)
async def test_mismatched_message_cannot_be_confirmed(setup, change):
    store, api, service = setup
    ident, expected = uncertain(store)
    api.call.return_value["messages"][0].update(change)
    with pytest.raises(Conflict):
        await service.resolve(1, ident, expected, "CONFIRM_MESSAGE", "Проверил сообщение вручную", "sent")
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
    assert not store.rows("SELECT * FROM operation_resolutions")


async def test_search_is_bounded_and_absence_never_requeues(setup):
    store, api, service = setup
    ident, expected = uncertain(store)

    async def page(*args, **kwargs):
        cursor = int(kwargs.get("after_cursor") or 0)
        return {"messages": [], "page_info": {"has_next_page": True, "end_cursor": str(cursor + 1)}}

    api.call.side_effect = page
    with pytest.raises(Conflict, match="не доказывает"):
        await service.resolve(1, ident, expected, "CONFIRM_MESSAGE", "Не вижу сообщение в чате", "absent")
    assert api.call.await_count == 20
    assert store.claim() is None


async def test_owner_and_snapshot_checked_before_api(setup):
    store, api, service = setup
    ident, expected = uncertain(store)
    with pytest.raises(Conflict, match="Доступ"):
        await service.resolve(2, ident, expected, "CONFIRM_MESSAGE", "Foreign owner attempted", "sent")
    with pytest.raises(Conflict, match="изменилась"):
        await service.resolve(1, ident, "wrong", "CONFIRM_MESSAGE", "Stale snapshot attempted", "sent")
    api.call.assert_not_called()


@pytest.mark.parametrize("source", ["success", "late", "resolution"])
async def test_one_message_receipt_cannot_confirm_two_operations(setup, source):
    store, api, service = setup
    first = store.enqueue("first", "reply", "chat", {"text": "private code"})
    second = store.enqueue("second", "reply", "chat", {"text": "private code"})
    action = store.claim()
    store.finish(
        first,
        action["lease_token"],
        "SUCCEEDED" if source == "success" else "UNCERTAIN",
        result={"message_id": "sent"} if source == "success" else None,
    )
    action = store.claim()
    store.finish(second, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
    api.call.return_value["messages"][0]["created_at"] = now()
    if source == "late":
        store.record_late_result(first, "old-lease", "SUCCEEDED", result={"message_id": "sent"})
    if source == "resolution":
        await service.resolve(
            1,
            first,
            signature(store.rows("SELECT * FROM outbox WHERE id=?", (first,))[0]),
            "CONFIRM_MESSAGE",
            "Проверена первая отправка",
            "sent",
        )
    expected = signature(store.rows("SELECT * FROM outbox WHERE id=?", (second,))[0])
    with pytest.raises(Conflict):
        await service.resolve(1, second, expected, "CONFIRM_MESSAGE", "Проверена вторая отправка", "sent")
    assert store.rows("SELECT status FROM outbox WHERE id=?", (second,)) == [{"status": "UNCERTAIN"}]


async def test_concurrent_message_resolution_cannot_share_one_receipt(setup):
    store, api, service = setup
    ids = [store.enqueue(key, "reply", "chat", {"text": "private code"}) for key in ("one", "two")]
    for _ in ids:
        action = store.claim()
        store.finish(action["id"], action["lease_token"], "UNCERTAIN")
    page, barrier, reads = api.call.return_value, asyncio.Event(), 0
    page["messages"][0]["created_at"] = now()

    async def read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), 2)
        return page

    api.call.side_effect = read
    results = await asyncio.gather(
        *(
            service.resolve(
                1,
                ident,
                signature(store.rows("SELECT * FROM outbox WHERE id=?", (ident,))[0]),
                "CONFIRM_MESSAGE",
                "Проверен точный ID сообщения",
                "sent",
            )
            for ident in ids
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, Conflict) for result in results) == 1


def delivery(store):
    orders = Orders(store)
    order = {
        "id": "order",
        "status": "PAID",
        "direction": "OUT",
        "item": {"id": "item", "price": 100},
        "chat": {"id": "chat"},
    }
    orders.ingest(order)
    orders.add_stock("item", ["private code"])
    orders.reserve("order")
    action = store.claim()
    store.finish(action["id"], action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
    return order, action["id"], signature(store.rows("SELECT * FROM outbox")[0])


async def test_delivery_confirmation_updates_quarantine_atomically_but_does_not_complete_deal(setup):
    store, api, service = setup
    order, ident, expected = delivery(store)
    messages = api.call.return_value
    api.call.side_effect = lambda method, **kwargs: order if method == "get_deal" else messages
    await service.resolve(1, ident, expected, "CONFIRM_MESSAGE", "Код в моём сообщении совпадает", "sent")
    assert store.rows("SELECT status FROM inventory")[0]["status"] == "DELIVERED"
    assert store.rows("SELECT status FROM orders")[0]["status"] == "PAID"
    assert all(call.args[0].startswith("get_") for call in api.call.call_args_list)
    assert [call.kwargs for call in api.call.call_args_list if call.args[0] == "get_deal"] == [
        {"deal_id": "order"}
    ]


async def test_receipt_of_another_reply_cannot_release_stock_quarantine(setup):
    store, api, service = setup
    order, ident, expected = delivery(store)
    known = store.enqueue("known-reply", "reply", "chat", {"text": "private code"})
    action = store.claim()
    store.finish(known, action["lease_token"], "SUCCEEDED", result={"message_id": "sent"})
    messages = api.call.return_value
    messages["messages"][0]["created_at"] = now()
    api.call.side_effect = lambda method, **kwargs: order if method == "get_deal" else messages
    with pytest.raises(Conflict):
        await service.resolve(1, ident, expected, "CONFIRM_MESSAGE", "Проверка квитанции выдачи", "sent")
    assert store.rows("SELECT status FROM inventory") == [{"status": "QUARANTINED"}]
    assert store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "UNCERTAIN"}]


async def test_resolution_transaction_rolls_back_with_quarantine_on_storage_failure(setup):
    store, api, service = setup
    order, ident, expected = delivery(store)
    messages = api.call.return_value
    api.call.side_effect = lambda method, **kwargs: order if method == "get_deal" else messages
    store.execute(
        "CREATE TRIGGER resolution_fail BEFORE UPDATE ON outbox BEGIN SELECT RAISE(ABORT,'test failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await service.resolve(1, ident, expected, "CONFIRM_MESSAGE", "Код в моём сообщении совпадает", "sent")
    assert store.rows("SELECT status FROM inventory")[0]["status"] == "QUARANTINED"
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
    assert not store.rows("SELECT * FROM operation_resolutions")


async def test_stop_keeps_quarantine_and_cannot_reserve_same_code_again(setup):
    store, api, service = setup
    _, ident, expected = delivery(store)
    await service.resolve(1, ident, expected, "STOP_UNCERTAIN", "Останавливаю без повторной отправки")
    api.call.assert_not_called()
    assert store.rows("SELECT status FROM inventory")[0]["status"] == "QUARANTINED"
    assert store.rows("SELECT error_code FROM outbox")[0]["error_code"] == "MANUALLY_STOPPED_UNCERTAIN"
    assert store.claim() is None
    Orders(store).reserve("order")
    assert len(store.rows("SELECT * FROM outbox")) == 1


async def test_read_state_requires_fresh_zero_counter(setup):
    store, api, service = setup
    ident, expected = uncertain(store, "mark_read", payload={})
    api.call.return_value = {"id": "chat", "unread_messages_counter": 1}
    with pytest.raises(Conflict):
        await service.resolve(1, ident, expected, "CONFIRM_READ_STATE", "Чат проверен вручную владельцем")
    api.call.return_value["unread_messages_counter"] = 0
    await service.resolve(1, ident, expected, "CONFIRM_READ_STATE", "Чат проверен вручную владельцем")
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"


def test_two_connections_cannot_resolve_the_same_operation_twice(setup):
    store, _, _ = setup
    ident, expected = uncertain(store)
    barrier = Barrier(2)

    def resolve(_):
        other = Store(store.path)
        try:
            barrier.wait(timeout=10)
            asyncio.run(
                Reconciliation(other, None).resolve(
                    1, ident, expected, "STOP_UNCERTAIN", "Проверено, остановлено без повтора"
                )
            )
            return "ok"
        except Conflict:
            return "conflict"
        finally:
            other.close()

    with ThreadPoolExecutor(2) as executor:
        assert sorted(executor.map(resolve, range(2))) == ["conflict", "ok"]
    assert len(store.rows("SELECT * FROM operation_resolutions")) == 1


async def test_stopped_unknown_photo_keeps_evidence_file(setup, tmp_path):
    store, _, service = setup
    attachments = Attachments(tmp_path / "uploads")
    attachments.directory.mkdir()
    source = attachments.directory / "proof.jpg"
    source.write_bytes(b"test")
    ident, expected = uncertain(store, payload={"text": "", "photos": [str(source)]})
    await service.resolve(1, ident, expected, "STOP_UNCERTAIN", "Файл сохранён для дальнейшей проверки")
    assert UploadCleanup(store, attachments, grace_seconds=-1).once()["removed"] == 0
    assert source.exists()


async def test_stop_uncertain_publish_never_releases_the_launch(setup):
    store, _, service = setup
    # A launch publication that became UNCERTAIN; its draft is stuck in QUEUED.
    store.draft(1, {"launch_target": "item", "account_id": "seller", "mode": "gifts"})
    draft_id = store.rows("SELECT id FROM drafts")[0]["id"]
    store.execute("UPDATE drafts SET state='QUEUED' WHERE id=?", (draft_id,))
    ident = store.enqueue(
        "launch:" + draft_id + ":0",
        "publish",
        "item",
        {"priority_id": "free", "price": 100, "raw_price": 100, "launch_id": draft_id},
    )
    action = store.claim()
    store.finish(ident, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
    store.set_setting("launch:prepared:item", {"id": draft_id, "owner": 1, "revision": 0, "operation": ident})
    expected = signature(store.rows("SELECT * FROM outbox WHERE id=?", (ident,))[0])
    await service.resolve(1, ident, expected, "STOP_UNCERTAIN", "Запрос не начинался, остановлено")
    # The owner stopping retries does NOT prove the publish never reached the server:
    # the launch stays confirmed-blocked (QUEUED + marker) until a real reconciliation.
    assert store.get_draft(draft_id, 1)["state"] == "QUEUED"
    assert store.setting("launch:prepared:item")["operation"] == ident
    # No new launch operation can be queued for the same revision while blocked.
    again = store.enqueue(
        "launch:" + draft_id + ":0",
        "publish",
        "item",
        {"priority_id": "free", "price": 100, "raw_price": 100, "launch_id": draft_id},
    )
    assert again == ident


async def test_stop_uncertain_premium_without_started_at_releases_proven_never_sent(setup):
    store, _, service = setup
    from app.services.paid_promotions import intent_key

    # A PREMIUM launch publication became UNCERTAIN; the payment intent exists but
    # was never started (no started_at) — the send provably never began.
    store.draft(1, {"launch_target": "item", "account_id": "seller", "mode": "manual"})
    draft_id = store.rows("SELECT id FROM drafts")[0]["id"]
    store.execute("UPDATE drafts SET state='QUEUED' WHERE id=?", (draft_id,))
    ident = store.enqueue(
        "launch:" + draft_id + ":0",
        "premium_publish",
        "item",
        {"priority_id": "premium", "price": 100, "launch_id": draft_id, "quote_id": "premium"},
    )
    action = store.claim()
    store.finish(ident, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
    store.set_setting(intent_key(ident), {"operation_id": ident, "pending": True})
    store.set_setting("launch:prepared:item", {"id": draft_id, "owner": 1, "revision": 0, "operation": ident})
    expected = signature(store.rows("SELECT * FROM outbox WHERE id=?", (ident,))[0])
    await service.resolve(1, ident, expected, "STOP_UNCERTAIN", "Оплата не начиналась, остановлено")
    # Proven never-sent: the launch is released for a fresh owner attempt.
    assert store.get_draft(draft_id, 1)["state"] == "DRAFT"
    assert not store.setting("launch:prepared:item")


async def test_stop_uncertain_premium_with_started_at_never_releases(setup):
    store, _, service = setup
    from app.services.paid_promotions import intent_key

    # The payment intent WAS started: the request may have reached the server.
    store.draft(1, {"launch_target": "item", "account_id": "seller", "mode": "manual"})
    draft_id = store.rows("SELECT id FROM drafts")[0]["id"]
    store.execute("UPDATE drafts SET state='QUEUED' WHERE id=?", (draft_id,))
    ident = store.enqueue(
        "launch:" + draft_id + ":0",
        "premium_publish",
        "item",
        {"priority_id": "premium", "price": 100, "launch_id": draft_id, "quote_id": "premium"},
    )
    action = store.claim()
    store.finish(ident, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
    store.set_setting(
        intent_key(ident), {"operation_id": ident, "started_at": "2026-11-01T00:00:00+00:00", "pending": True}
    )
    store.set_setting("launch:prepared:item", {"id": draft_id, "owner": 1, "revision": 0, "operation": ident})
    expected = signature(store.rows("SELECT * FROM outbox WHERE id=?", (ident,))[0])
    await service.resolve(1, ident, expected, "STOP_UNCERTAIN", "Оплата могла начаться, остановлено")
    # Unknown external result NEVER becomes permission to retry: the launch stays blocked.
    assert store.get_draft(draft_id, 1)["state"] == "QUEUED"
    assert store.setting("launch:prepared:item")["operation"] == ident


def test_upgrade_backfills_existing_unknown_operation_once_without_exposing_payload(tmp_path):
    path = tmp_path / "upgrade.sqlite"
    store = Store(path)
    ident, _ = uncertain(store)
    # Emulate a pre-v6 database: the action exists, but alert generation did not.
    store.execute("DELETE FROM schema_migrations WHERE version=6")
    store.execute("DELETE FROM inbox WHERE kind='operation_uncertain'")
    store.close()
    store = Store(path)
    try:
        events = store.rows("SELECT payload FROM inbox WHERE kind='operation_uncertain'")
        assert len(events) == 1 and ident in events[0]["payload"]
        assert "private code" not in events[0]["payload"]
        assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
    finally:
        store.close()
    store = Store(path)
    try:
        assert len(store.rows("SELECT * FROM inbox WHERE kind='operation_uncertain'")) == 1
    finally:
        store.close()
