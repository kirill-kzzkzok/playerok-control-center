"""Connected stock pipeline; only the remote marketplace is simulated."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict

import pytest

from app.database.store import Store, now
from app.playerok.adapter import APIError
from app.services.automation import Automation, DeliveryPolicy
from app.services.fulfillment import Fulfillment
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.outbox import Executor, OutboxWorker
from app.workers.sync import Synchronizer


class ProcessLost(BaseException):
    pass


class Marketplace:
    id, writes_enabled, automation_enabled = "seller", True, True

    def __init__(self, outcome):
        self.account = self
        self.lock = asyncio.Lock()
        self.outcome, self.orders, self.messages, self.mutations = outcome, {}, [], []

    @contextmanager
    def permit(self, operation):
        assert operation in {"createChatMessage", "updateDeal"}
        yield

    @staticmethod
    def page(key, values):
        return {key: deepcopy(values), "page_info": {"has_next_page": False, "end_cursor": None}}

    async def call(self, method, **kwargs):
        if method == "get_deals":
            return self.page("deals", list(self.orders.values()))
        if method == "get_chats":
            return self.page("chats", [{"id": "chat"}] if self.orders else [])
        if method == "get_chat_messages":
            assert kwargs["chat_id"] == "chat"
            return self.page("messages", self.messages)
        if method == "get_item":
            return {"id": "item", "user": {"id": self.id}}
        if method == "get_deal":
            return deepcopy(self.orders[kwargs["deal_id"]])
        if method == "get_chat":
            return {"id": "chat", "is_texting_allowed": True}
        raise AssertionError(method)

    def paid_order(self):
        self.orders["order"] = {
            "id": "order",
            "status": "PAID",
            "direction": "OUT",
            "has_problem": False,
            "is_automated": False,
            "created_at": now(),
            "item": {"id": "item", "user": {"id": self.id, "role": "USER"}},
            "chat": {"id": "chat"},
        }

    def send_message(self, chat_id, text):
        assert chat_id == "chat"
        self.mutations.append("send")
        message = {"id": "receipt", "text": text, "user": {"id": self.id}, "created_at": now()}
        self.messages.append(message)
        if self.outcome == "lost_ack":
            raise APIError("NETWORK_ERROR", uncertain=True)
        return {} if self.outcome == "missing_receipt" else deepcopy(message)

    def update_deal(self, deal_id, new_status):
        assert deal_id == "order" and new_status.name == "SENT"
        self.mutations.append("fulfill")
        self.orders[deal_id]["status"] = "SENT"
        return deepcopy(self.orders[deal_id])


@pytest.mark.parametrize(
    "outcome", ["success", "lost_ack", "missing_receipt", "crash_before_send", "crash_after_send"]
)
async def test_poll_discover_reserve_restart_send_receipt_and_fulfillment(tmp_path, outcome):
    path = tmp_path / "stock.sqlite"
    store, api = Store(path), Marketplace(outcome)
    try:
        store.claim_first_owner(1)
        sync = Synchronizer(store, api, Orders(store))
        await sync.once()  # Real baseline machinery, no fabricated readiness setting.
        assert store.setting("orders:last_complete_sync")
        await Automation(store, api).save(
            1,
            "item",
            0,
            asdict(DeliveryPolicy(enabled=True, mode="stock", auto_enroll=True, mark_sent=True)),
            future_confirmed=True,
        )
        assert Orders(store).add_stock("item", ["fixture unique unit", "fixture unique unit"]) == 1
        api.paid_order()
        await sync.once()  # Discovery through get_deals, not direct Orders.ingest.
        sync.request("deal", "order")
        sync.request("deal", "order")
        await sync.process_requests()
        await sync.once()  # Repeated polling and duplicate event hints.
        assert await EnrollmentWorker(store, api).once() == 1
        assert await EnrollmentWorker(store, api).once() == 0
        assert store.rows("SELECT status FROM inventory") == [{"status": "RESERVED"}]
        assert len(store.rows("SELECT * FROM reservations")) == 1
        assert ChainWorker(store, api).once() == 1
        assert ChainWorker(store, api).once() == 0
        assert not api.messages
        if outcome == "crash_before_send":
            assert store.claim()  # Process dies with an admitted action, before HTTP.

        store.close()
        store = Store(path)
        worker = OutboxWorker(store, Executor(store, api, None))
        if outcome == "crash_before_send":
            store.execute("UPDATE outbox SET lease_until=0 WHERE status='PROCESSING'")
            assert store.recover() == 1
        elif outcome == "crash_after_send":

            def crash(*args, **kwargs):
                raise ProcessLost()

            worker.finish = crash
            with pytest.raises(ProcessLost):
                await worker.once()
            store.close()
            store = Store(path)
            store.execute("UPDATE outbox SET lease_until=0 WHERE status='PROCESSING'")
            assert store.recover() == 1
        else:
            assert await worker.once()

        ChainWorker(store, api).once()
        store.close()
        store = Store(path)
        worker = OutboxWorker(store, Executor(store, api, None))
        sync = Synchronizer(store, api, Orders(store))
        await sync.once()  # Independent chat-history read and repeated order discovery.
        await EnrollmentWorker(store, api).once()
        ChainWorker(store, api).once()
        assert not await worker.once()
        assert len(store.rows("SELECT * FROM reservations")) == 1
        assert len(store.rows("SELECT * FROM delivery_chains")) == 1
        assert len(api.messages) == (0 if outcome == "crash_before_send" else 1)
        assert not store.rows("SELECT 1 FROM inventory WHERE status='AVAILABLE'")

        if outcome != "success":
            assert store.rows("SELECT status FROM inventory") == [{"status": "QUARANTINED"}]
            assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
            assert store.rows("SELECT status FROM delivery_chains") == [{"status": "BLOCKED"}]
            assert store.rows("SELECT status FROM orders") == [{"status": "PAID"}]
            assert "fulfill" not in api.mutations
            return

        assert store.rows("SELECT status FROM inventory") == [{"status": "DELIVERED"}]
        assert store.rows("SELECT status FROM delivery_chains") == [{"status": "NEEDS_FULFILLMENT"}]
        receipt = json.loads(store.rows("SELECT result FROM outbox")[0]["result"])
        message = json.loads(
            store.rows("SELECT payload FROM chat_messages WHERE id=?", (receipt["message_id"],))[0]["payload"]
        )
        assert message["text"] == "fixture unique unit" and message["user"]["id"] == api.id
        assert not store.rows("SELECT 1 FROM outbox WHERE kind='fulfill'")
        service = Fulfillment(store, api)
        prepared = await service.prepare(1, "order")
        await service.queue(1, "order", prepared["snapshot"], {}, True, "owner-attestation")
        assert await worker.once()
        await sync.once()
        # A stale PAID replay must not undo the server-confirmed SENT state.
        Orders(store).ingest({**api.orders["order"], "status": "PAID"})
        assert store.rows("SELECT status FROM orders") == [{"status": "SENT"}]
        assert api.mutations == ["send", "fulfill"]
        assert not await worker.once()
    finally:
        store.close()


@pytest.mark.parametrize("same_order", [True, False])
def test_two_database_connections_cannot_reserve_last_unit_twice(tmp_path, same_order):
    path = tmp_path / "competition.sqlite"
    store = Store(path)
    try:
        for ident in ("one", "two"):
            Orders(store).ingest(
                {
                    "id": ident,
                    "status": "PAID",
                    "direction": "OUT",
                    "item": {"id": "item"},
                    "chat": {"id": ident},
                }
            )
        Orders(store).add_stock("item", ["fixture last unit"])
        barrier = threading.Barrier(2)

        def reserve(ident):
            connection = Store(path)
            try:
                barrier.wait(timeout=5)
                return Orders(connection).reserve(ident)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(reserve, "one")
            second = pool.submit(reserve, "one" if same_order else "two")
            results = [first.result(timeout=10), second.result(timeout=10)]
        assert (results[0] == results[1]) if same_order else (results.count(None) == 1)
        assert len(store.rows("SELECT * FROM reservations")) == 1
        assert len(store.rows("SELECT * FROM outbox")) == 1
        assert store.rows("SELECT status FROM inventory") == [{"status": "RESERVED"}]
    finally:
        store.close()
