"""Production stock chain integration; only marketplace transport is simulated.

The registry projection is prepared directly; this does not certify Telegram UI.
"""

from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from app.database.store import Store, encode, now
from app.playerok.adapter import APIError
from app.services.automation import DeliveryPolicy
from app.services.inventory import Inventory
from app.services.logical_products import critical, digest
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.outbox import Executor, OutboxWorker
from app.workers.sync import Synchronizer
from tests.test_stock_auto_cycle import Marketplace, ProcessLost


def listing(ident):
    return {
        "id": ident,
        "user": {"id": "seller"},
        "name": "Steam account product [AB6]",
        "description": "Actual product description",
        "price": 100,
        "category": {"id": "steam-category"},
        "obtaining_type": {"id": "account-transfer"},
        "data_fields": [],
        "status": "APPROVED",
    }


class AliasMarketplace(Marketplace):
    def __init__(self, outcome="success"):
        super().__init__(outcome)
        self.attempts = []

    def paid_order(self, ident, item="relisted"):
        self.orders[ident] = {
            "id": ident,
            "status": "PAID",
            "direction": "OUT",
            "has_problem": False,
            "is_automated": False,
            "created_at": now(),
            "item": {"id": item, "user": {"id": self.id, "role": "USER"}},
            "chat": {"id": "chat-" + ident},
        }

    async def call(self, method, **kwargs):
        if method == "get_chats":
            return self.page("chats", [{"id": order["chat"]["id"]} for order in self.orders.values()])
        if method == "get_chat":
            return {"id": kwargs["chat_id"], "is_texting_allowed": True}
        if method == "get_chat_messages":
            return self.page("messages", [m for m in self.messages if m["chat_id"] == kwargs["chat_id"]])
        if method == "get_item":
            return listing(kwargs["id"])
        return await super().call(method, **kwargs)

    def send_message(self, chat_id, text):
        self.attempts.append((chat_id, text))
        if self.outcome == "timeout_before_delivery":
            raise APIError("NETWORK_ERROR", uncertain=True)
        message = {
            "id": "receipt-" + str(len(self.messages) + 1),
            "chat_id": chat_id,
            "text": text,
            "user": {"id": self.id},
            "created_at": now(),
        }
        self.messages.append(message)
        if self.outcome == "timeout_after_delivery":
            raise APIError("NETWORK_ERROR", uncertain=True)
        return deepcopy(message)


def registry(store):
    store.claim_first_owner(1)
    store.set_setting("playerok:account_id", "seller")
    armed = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    config = encode({"after": "Thank you for this purchase"})
    store.execute(
        "INSERT INTO logical_products(id,code,owner_id,account_id,handler,config,state,enabled,armed_at,stock_item_id,baseline,original_description,created_at,updated_at) "
        "VALUES('stock-product','AB6',1,'seller','unique_stock',?,'VERIFIED',1,?,'original',?,?,?,?)",
        (
            config,
            armed,
            encode(critical(listing("original"))),
            listing("original")["description"],
            armed,
            armed,
        ),
    )
    policy = asdict(
        DeliveryPolicy(
            enabled=True, mode="stock", after="Thank you for this purchase", auto_enroll=True, mark_sent=True
        )
    )
    for ident in ("original", "relisted"):
        store.execute(
            "INSERT INTO logical_listings(item_id,product_id,config_revision,description_sha256,server_status,verified_at) "
            "VALUES(?,'stock-product',1,?,'APPROVED',?)",
            (ident, digest(listing(ident)["description"]), now()),
        )
        store.execute(
            "UPDATE logical_listings SET title_snapshot=? WHERE item_id=?", (listing(ident)["name"], ident)
        )
        store.execute("INSERT INTO delivery_policies VALUES(?,1,1,?,?)", (ident, encode(policy), armed))


async def drain(store, api):
    worker = OutboxWorker(store, Executor(store, api, None))
    for _ in range(12):
        advanced = ChainWorker(store, api).once()
        performed = await worker.once()
        if not advanced and not performed:
            return
    raise AssertionError("Chain did not settle within its bounded steps")


async def test_three_alias_sales_get_distinct_stock_and_one_thanks_each_then_empty(tmp_path):
    path = tmp_path / "alias-chain.sqlite3"
    api, store = AliasMarketplace(), Store(path)
    try:
        registry(store)
        await Synchronizer(store, api, Orders(store)).once()
        assert (
            Orders(store).add_stock(
                "original", ["fixture account A", "fixture account B", "fixture account C"]
            )
            == 3
        )
        for ident in ("one", "two", "three"):
            api.paid_order(ident)
            await Synchronizer(store, api, Orders(store)).once()
            assert await EnrollmentWorker(store, api).once() == 1
            assert len(store.rows("SELECT * FROM reservations WHERE order_id=?", (ident,))) == 1
            if ident == "three":
                assert store.setting("delivery:original:enabled") is None
                assert store.setting("delivery:relisted:enabled") is None
            await drain(store, api)
            assert store.rows("SELECT status FROM delivery_chains WHERE order_id=?", (ident,)) == [
                {"status": "NEEDS_FULFILLMENT"}
            ]
            assert [m["text"] for m in api.messages if m["chat_id"] == "chat-" + ident][
                -1
            ] == "Thank you for this purchase"
            # Restart after each completed delivery, including the last reserve
            # which was dispatched after stock-empty stopped new admission.
            store.close()
            store = Store(path)
            await Synchronizer(store, api, Orders(store)).once()
            assert await EnrollmentWorker(store, api).once() == 0
            await drain(store, api)
        delivered = [m["text"] for m in api.messages if m["text"].startswith("fixture account ")]
        assert delivered == ["fixture account A", "fixture account B", "fixture account C"]
        assert len(api.messages) == 6 and len(api.attempts) == 6
        assert len(store.rows("SELECT * FROM reservations")) == 3
        assert Inventory(store, api).counts("relisted") == {
            "AVAILABLE": 0,
            "RESERVED": 0,
            "DELIVERED": 3,
            "QUARANTINED": 0,
        }
        assert len(store.rows("SELECT * FROM logical_order_scopes")) == 3
        assert not store.rows("SELECT * FROM outbox WHERE kind='fulfill'")
        api.paid_order("four")
        await Synchronizer(store, api, Orders(store)).once()
        await EnrollmentWorker(store, api).once()
        await drain(store, api)
        assert not store.rows("SELECT * FROM reservations WHERE order_id='four'")
        assert len(api.messages) == 6 and len(api.attempts) == 6
        assert not store.rows("SELECT * FROM outbox WHERE target='four'")
        assert store.rows("SELECT status FROM orders WHERE id='four'") == [{"status": "PAID"}]
    finally:
        store.close()


@pytest.mark.parametrize("outcome", ["timeout_before_delivery", "timeout_after_delivery"])
async def test_alias_delivery_unknown_quarantines_exact_unit_and_never_sends_next_or_thanks(
    tmp_path, outcome
):
    path = tmp_path / "unknown-alias.sqlite3"
    store, api = Store(path), AliasMarketplace(outcome)
    try:
        registry(store)
        await Synchronizer(store, api, Orders(store)).once()
        Orders(store).add_stock("original", ["fixture account A", "fixture account B"])
        api.paid_order("unknown")
        await Synchronizer(store, api, Orders(store)).once()
        assert await EnrollmentWorker(store, api).once() == 1
        await drain(store, api)
        assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
        assert store.rows("SELECT status FROM delivery_chains") == [{"status": "BLOCKED"}]
        assert Inventory(store, api).counts("relisted") == {
            "AVAILABLE": 1,
            "RESERVED": 0,
            "DELIVERED": 0,
            "QUARANTINED": 1,
        }
        assert len(api.attempts) == 1
        assert len(api.messages) == (1 if outcome == "timeout_after_delivery" else 0)
        store.close()
        store = Store(path)
        api.outcome = "success"
        for _ in range(2):
            await Synchronizer(store, api, Orders(store)).once()
            await EnrollmentWorker(store, api).once()
            await drain(store, api)
        assert len(api.attempts) == 1
        assert not any(m["text"] == "Thank you for this purchase" for m in api.messages)
        assert len(store.rows("SELECT * FROM reservations")) == 1
        assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
    finally:
        store.close()


async def test_alias_process_lost_after_message_ack_does_not_repeat_after_lease_recovery(tmp_path):
    path = tmp_path / "crashed-alias.sqlite3"
    store, api = Store(path), AliasMarketplace()
    try:
        registry(store)
        await Synchronizer(store, api, Orders(store)).once()
        Orders(store).add_stock("original", ["fixture account A", "fixture account B"])
        api.paid_order("crash")
        await Synchronizer(store, api, Orders(store)).once()
        assert await EnrollmentWorker(store, api).once() == 1
        assert ChainWorker(store, api).once() == 1
        worker = OutboxWorker(store, Executor(store, api, None))

        def crash(*args, **kwargs):
            raise ProcessLost()

        worker.finish = crash
        with pytest.raises(ProcessLost):
            await worker.once()
        assert len(api.messages) == 1 and len(api.attempts) == 1
        assert store.rows("SELECT status FROM outbox") == [{"status": "PROCESSING"}]
        store.close()
        store = Store(path)
        store.execute("UPDATE outbox SET lease_until=0 WHERE status='PROCESSING'")
        assert store.recover() == 1
        await Synchronizer(store, api, Orders(store)).once()
        await EnrollmentWorker(store, api).once()
        await drain(store, api)
        assert len(api.messages) == 1 and len(api.attempts) == 1
        assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
        assert store.rows("SELECT status FROM delivery_chains") == [{"status": "BLOCKED"}]
        assert Inventory(store, api).counts("relisted") == {
            "AVAILABLE": 1,
            "RESERVED": 0,
            "DELIVERED": 0,
            "QUARANTINED": 1,
        }
    finally:
        store.close()
