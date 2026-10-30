import asyncio
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.store import Conflict, Store
from app.playerok.adapter import APIError
from app.services.automation import Automation, DeliveryPolicy
from app.services.delivery_chains import DeliveryChains
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.outbox import Executor, OutboxWorker


class Account:
    id, writes_enabled, automation_enabled = "seller", True, True

    def __init__(self):
        self.sent, self.error = [], None

    @contextmanager
    def permit(self, operation):
        assert operation == "createChatMessage"
        yield

    def send_message(self, chat_id, text):
        assert chat_id == "chat"
        self.sent.append(text)
        if self.error:
            raise self.error
        return {"id": "message-" + str(len(self.sent))}


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "chains.sqlite")
    store.claim_first_owner(1)
    order = {
        "id": "order",
        "direction": "OUT",
        "status": "PAID",
        "has_problem": False,
        "is_automated": False,
        "item": {"id": "item", "user": {"id": "seller"}},
        "chat": {"id": "chat"},
    }

    async def call(method, **kwargs):
        if method == "get_deal":
            return order
        if method == "get_chat":
            return {"id": "chat", "is_texting_allowed": True}
        if method == "get_item":
            return {"id": "item", "user": {"id": "seller"}}
        raise AssertionError(method)

    api = SimpleNamespace(account=Account(), lock=asyncio.Lock(), call=AsyncMock(side_effect=call))
    yield store, api, order, DeliveryChains(store, api)
    store.close()


async def configure(store, api, *, mode="stock", mark=False):
    await Automation(store, api).save(
        1,
        "item",
        0,
        asdict(
            DeliveryPolicy(
                enabled=True, mode=mode, text="fixed secret", greeting="hello", after="thanks", mark_sent=mark
            )
        ),
    )
    Orders(store).add_stock("item", ["unique secret"])


@pytest.mark.parametrize("mode", ["stock", "text"])
async def test_ordered_durable_steps_no_repeat_or_automatic_confirm(setup, mode):
    store, api, _, service = setup
    await configure(store, api, mode=mode)
    assert await service.create(1, "order") == "order"
    assert await service.create(1, "order") == "order"
    assert not store.rows("SELECT * FROM outbox")
    scheduler, worker = ChainWorker(store, api), OutboxWorker(store, Executor(store, api, None))
    for _ in range(3):
        assert scheduler.once() == 1
        assert scheduler.once() == 0
        assert await worker.once()
    assert scheduler.once() == 1
    assert api.account.sent == ["hello", "unique secret" if mode == "stock" else "fixed secret", "thanks"]
    assert store.rows("SELECT status FROM delivery_chains")[0]["status"] == "SUCCEEDED"
    assert not await worker.once() and scheduler.once() == 0
    assert store.rows("SELECT status FROM orders")[0]["status"] == "PAID"
    assert "secret" not in str(store.rows("SELECT * FROM order_history"))


async def test_restart_resumes_queued_step_without_duplicate(setup):
    store, api, _, service = setup
    await configure(store, api)
    await service.create(1, "order")
    service.advance("order")
    other = Store(store.path)
    try:
        assert not DeliveryChains(other, api).advance("order")
        await OutboxWorker(other, Executor(other, api, None)).once()
        assert DeliveryChains(other, api).advance("order")
        assert len(other.rows("SELECT * FROM outbox")) == 2
    finally:
        other.close()


async def test_greeting_timeout_blocks_secret_and_quarantines_reserved_stock(setup):
    store, api, _, service = setup
    await configure(store, api)
    await service.create(1, "order")
    service.advance("order")
    api.account.error = APIError("NETWORK_ERROR", uncertain=True)
    worker = OutboxWorker(store, Executor(store, api, None))
    await worker.once()
    service.advance("order")
    assert not await worker.once()
    assert api.account.sent == ["hello"]
    assert store.rows("SELECT status FROM inventory") == [{"status": "QUARANTINED"}]
    assert store.rows("SELECT status FROM delivery_chains")[0]["status"] == "BLOCKED"


async def test_disable_policy_after_queue_prevents_even_first_send(setup):
    store, api, _, service = setup
    await configure(store, api)
    await service.create(1, "order")
    service.advance("order")
    Automation(store, api).stop_all(1)
    worker = OutboxWorker(store, Executor(store, api, None))
    await worker.once()
    service.advance("order")
    assert not api.account.sent
    assert store.rows("SELECT status FROM delivery_chains")[0]["status"] == "CANCELLED"
    assert store.rows("SELECT status FROM inventory") == [{"status": "QUARANTINED"}]


async def test_return_after_greeting_blocks_delivery(setup):
    store, api, order, service = setup
    await configure(store, api)
    await service.create(1, "order")
    worker = OutboxWorker(store, Executor(store, api, None))
    service.advance("order")
    await worker.once()
    order["status"] = "ROLLED_BACK"
    service.advance("order")
    await worker.once()
    service.advance("order")
    assert api.account.sent == ["hello"]
    assert store.rows("SELECT status FROM inventory") == [{"status": "QUARANTINED"}]


async def test_manual_delivery_cannot_bypass_existing_text_chain(setup):
    store, api, _, service = setup
    await configure(store, api, mode="text")
    await service.create(1, "order")
    with pytest.raises(Conflict):
        Orders(store).reserve("order")
    assert not store.rows("SELECT * FROM reservations")
    assert store.rows("SELECT status FROM inventory") == [{"status": "AVAILABLE"}]


async def test_mark_sent_requires_followup_fulfillment_workflow(setup):
    store, api, _, service = setup
    await configure(store, api, mark=True)
    await service.create(1, "order")
    worker = OutboxWorker(store, Executor(store, api, None))
    for _ in range(3):
        service.advance("order")
        await worker.once()
    service.advance("order")
    assert store.rows("SELECT status FROM delivery_chains")[0]["status"] == "NEEDS_FULFILLMENT"
    assert not store.rows("SELECT * FROM outbox WHERE kind='fulfill'")


async def test_disabled_global_runtime_and_stale_policy_create_no_reservation(setup):
    store, api, _, service = setup
    await configure(store, api)
    api.account.automation_enabled = False
    with pytest.raises(ValueError, match="выключено"):
        await service.create(1, "order")
    api.account.automation_enabled = True
    with pytest.raises(Conflict, match="изменилась"):
        await service.create(1, "order", expected_revision=0)
    assert not store.rows("SELECT * FROM reservations") and not store.rows("SELECT * FROM outbox")


async def test_policy_changes_during_api_validation_prevent_send(setup):
    store, api, _, service = setup
    await configure(store, api)
    await service.create(1, "order")
    service.advance("order")
    original = api.call.side_effect

    async def call(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_chat":
            Automation(store, api).stop_all(1)
        return result

    api.call.side_effect = call
    await OutboxWorker(store, Executor(store, api, None)).once()
    assert not api.account.sent
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "PERMANENT_FAILED"


@pytest.mark.parametrize("change", ["policy", "runtime", "problem", "rollback", "expired", "recovered"])
async def test_stop_while_waiting_for_api_lock_prevents_dispatch(setup, change):
    store, api, order, service = setup
    await configure(store, api)
    await service.create(1, "order")
    service.advance("order")
    checked = asyncio.Event()
    original = api.call.side_effect

    async def call(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_chat":
            checked.set()
        return result

    api.call.side_effect = call
    await api.lock.acquire()
    task = asyncio.create_task(OutboxWorker(store, Executor(store, api, None)).once())
    try:
        await asyncio.wait_for(checked.wait(), 2)
        if change == "policy":
            Automation(store, api).stop_all(1)
        elif change == "runtime":
            api.account.automation_enabled = False
        elif change == "problem":
            Orders(store).annotate("order", 1, 0, True, "Needs checking")
        elif change == "rollback":
            Orders(store).ingest({**order, "status": "ROLLED_BACK"})
        else:
            store.execute("UPDATE outbox SET lease_until=0 WHERE status='PROCESSING'")
            if change == "recovered":
                store.recover()
    finally:
        api.lock.release()
    await task
    assert not api.account.sent


async def test_expired_lease_after_external_ack_keeps_late_receipt_without_retry(setup):
    store, api, _, service = setup
    await configure(store, api)
    await service.create(1, "order")
    service.advance("order")
    accepted, release = threading.Event(), threading.Event()

    def send(chat_id, text):
        api.account.sent.append(text)
        accepted.set()
        assert release.wait(5)
        return {"id": "external-ack"}

    api.account.send_message = send
    task = asyncio.create_task(OutboxWorker(store, Executor(store, api, None)).once())
    try:
        assert await asyncio.to_thread(accepted.wait, 3)
        store.execute("UPDATE outbox SET lease_until=? WHERE status='PROCESSING'", (time.time() - 1,))
        assert store.recover() == 1
    finally:
        release.set()
    await task
    service.advance("order")
    assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
    observations = store.rows("SELECT observed_status,result FROM outbox_late_results")
    assert observations == [{"observed_status": "SUCCEEDED", "result": '{"message_id":"external-ack"}'}]
    assert not await OutboxWorker(store, Executor(store, api, None)).once()
    assert api.account.sent == ["hello"]
    assert store.rows("SELECT status FROM inventory") == [{"status": "QUARANTINED"}]
    assert "unique secret" not in str(store.rows("SELECT * FROM outbox_late_results"))
