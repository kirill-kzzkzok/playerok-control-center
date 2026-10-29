from concurrent.futures import ThreadPoolExecutor

import pytest

from app.database.store import Conflict, Store
from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.workers.outbox import OutboxWorker


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "db.sqlite")
    yield value
    value.close()


def order(ident="order1", status="PAID"):
    return {
        "id": ident,
        "status": status,
        "direction": "OUT",
        "item": {"id": "item1", "price": 100},
        "chat": {"id": "chat1"},
        "created_at": "2026-10-29T10:00:00+00:00",
    }


def test_duplicate_and_parallel_events(store):
    service = Orders(store)
    with ThreadPoolExecutor(4) as executor:
        results = list(executor.map(lambda _: service.ingest(order()), range(20)))
    assert sum(results) == 1
    assert len(store.rows("SELECT * FROM inbox")) == 1
    assert len(store.rows("SELECT * FROM orders")) == 1


def test_parallel_reservation_and_duplicate_stock(store):
    service = Orders(store)
    service.ingest(order())
    assert service.add_stock("item1", ["secret1", "secret1", "secret2"]) == 2
    with ThreadPoolExecutor(4) as executor:
        reservations = list(executor.map(lambda _: service.reserve("order1"), range(10)))
    assert len(set(reservations)) == 1
    assert len(store.rows("SELECT * FROM outbox")) == 1


def test_stock_cannot_be_given_to_two_buyers(store):
    service = Orders(store)
    service.ingest(order("one"))
    service.ingest(order("two"))
    service.add_stock("item1", ["one code"])
    first = service.reserve("one")
    assert first is not None
    assert service.reserve("two") is None
    assert store.setting("delivery:item1:enabled") is False


@pytest.mark.parametrize("status", ["PENDING", "ROLLED_BACK", "CONFIRMED", "SENT"])
def test_unpaid_or_finished_orders_not_delivered(store, status):
    service = Orders(store)
    service.ingest(order(status=status))
    service.add_stock("item1", ["code"])
    with pytest.raises(Conflict):
        service.reserve("order1")


def test_terminal_status_not_downgraded(store):
    service = Orders(store)
    service.ingest(order(status="CONFIRMED"))
    assert not service.ingest(order(status="PAID"))
    assert store.rows("SELECT status FROM orders")[0]["status"] == "CONFIRMED"


def test_atomic_rollback_on_crash(store):
    with pytest.raises(RuntimeError), store.transaction() as db:
        db.execute("INSERT INTO settings VALUES('x','1')")
        raise RuntimeError("crash")
    assert store.setting("x") is None


def test_restart_after_external_call_quarantines_stock(store):
    service = Orders(store)
    service.ingest(order())
    service.add_stock("item1", ["code"])
    service.reserve("order1")
    action = store.claim(lease_seconds=-1)
    new = Store(store.path)
    try:
        assert new.recover() == 1
        assert new.rows("SELECT status FROM inventory")[0]["status"] == "QUARANTINED"
        assert new.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
        assert new.claim() is None
        with pytest.raises(Conflict):
            new.finish(action["id"], action["lease_token"], "SUCCEEDED")
    finally:
        new.close()


async def test_timeout_after_send_never_retries(store):
    calls = []

    async def send(action):
        calls.append(action)
        raise APIError("NETWORK_ERROR", uncertain=True)

    store.enqueue("reply:one", "reply", "chat", {"text": "hello"})
    worker = OutboxWorker(store, send)
    assert await worker.once()
    assert not await worker.once()
    assert len(calls) == 1
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"


async def test_success_commits_delivery_and_outbox_together(store):
    service = Orders(store)
    service.ingest(order())
    service.add_stock("item1", ["sensitive-code"])
    service.reserve("order1")

    async def send(action):
        return {"message_id": "external1"}

    await OutboxWorker(store, send).once()
    assert store.rows("SELECT status FROM inventory")[0]["status"] == "DELIVERED"
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
    assert "sensitive-code" not in str(store.rows("SELECT * FROM audit_log"))


def test_duplicate_messages_do_not_repeat_inbox(store):
    orders = Orders(store)
    message = {"id": "m1", "text": "hello"}
    assert orders.ingest_message("chat", message)
    assert not orders.ingest_message("chat", message)
    assert len(store.rows("SELECT * FROM chat_messages")) == 1
