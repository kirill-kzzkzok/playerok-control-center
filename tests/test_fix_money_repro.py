"""Independent money-liveness reproductions; real SQLite/Orders/Executor only."""

from dataclasses import asdict

import pytest

from app.database.store import Conflict, Store
from app.playerok.adapter import APIError
from app.services.automation import Automation, DeliveryPolicy
from app.services.orders import Orders
from app.services.products import Products
from app.workers.outbox import Executor, OutboxWorker
from tests.test_stock_auto_cycle import Marketplace


@pytest.fixture
def stock(tmp_path):
    store, api = Store(tmp_path / "money.sqlite"), Marketplace("success")
    store.claim_first_owner(1)
    api.paid_order()
    Orders(store).ingest(api.orders["order"])
    Orders(store).add_stock("item", ["isolated-secret"])
    yield store, api
    store.close()


@pytest.mark.parametrize("code", ["HTTP_503", "NETWORK_ERROR", "RATE_LIMITED", "CONNECTION_NOT_VERIFIED"])
async def test_preflight_503_preserves_reserved_unit_for_bounded_read_retry(stock, code):
    store, api = stock
    Orders(store).reserve("order", actor=1)
    original = api.call

    async def fail_read(method, **kwargs):
        if method == "get_deal":
            raise APIError(code)
        return await original(method, **kwargs)

    api.call = fail_read
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    assert not api.mutations
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "RETRYABLE_FAILED"
    assert store.rows("SELECT status FROM inventory")[0]["status"] == "RESERVED"
    assert len(store.rows("SELECT * FROM reservations")) == 1


def test_problem_annotation_prevents_reserve_without_touching_stock(stock):
    store, _api = stock
    Orders(store).annotate("order", 1, 0, True, "Owner problem")
    with pytest.raises((Conflict, ValueError)):
        Orders(store).reserve("order", actor=1)
    assert store.rows("SELECT status FROM inventory")[0]["status"] == "AVAILABLE"
    assert not store.rows("SELECT * FROM reservations")
    assert not store.rows("SELECT * FROM outbox")


async def test_manual_stock_reserve_cannot_disable_fixed_text_policy(stock):
    store, api = stock
    await Automation(store, api).save(
        1, "item", 0, asdict(DeliveryPolicy(enabled=True, mode="text", text="fixed-value"))
    )
    # No stock for this text listing; absence must not invoke empty_stock.
    store.execute("DELETE FROM inventory")
    before = store.rows("SELECT * FROM delivery_policies")
    with pytest.raises((Conflict, ValueError)):
        Orders(store).reserve("order", actor=1)
    assert store.setting("delivery:item:enabled") is True
    assert store.rows("SELECT * FROM delivery_policies") == before
    assert not store.rows("SELECT * FROM outbox")


async def test_safe_preflight_retry_uses_same_reserve_and_single_post(stock, monkeypatch):
    import time

    store, api = stock
    Orders(store).reserve("order", actor=1)
    original = api.call
    failures = 1

    async def flaky(method, **kwargs):
        nonlocal failures
        if method == "get_deal" and failures:
            failures -= 1
            raise APIError("HTTP_503")
        return await original(method, **kwargs)

    api.call = flaky
    worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
    await worker.once()
    assert not await worker.once()
    due = store.rows("SELECT retry_at FROM outbox")[0]["retry_at"]
    monkeypatch.setattr(time, "time", lambda: due + 1)
    await worker.once()
    assert api.mutations == ["send"]
    assert store.rows("SELECT status FROM inventory") == [{"status": "DELIVERED"}]
    assert not await worker.once()


def test_late_dispatch_cannot_cross_safe_finish_cas(stock):
    store, _api = stock
    Orders(store).reserve("order", actor=1)
    action = store.claim()
    store.finish(action["id"], action["lease_token"], "RETRYABLE_FAILED", error="HTTP_503")
    with pytest.raises(Conflict):
        store.mark_started(action)
    assert store.rows("SELECT status FROM inventory") == [{"status": "RESERVED"}]


def test_started_post_can_never_become_retryable_even_with_misclassified_http_error(stock):
    store, _api = stock
    Orders(store).reserve("order", actor=1)
    action = store.claim()
    store.mark_started(action)
    store.finish(action["id"], action["lease_token"], "RETRYABLE_FAILED", error="HTTP_429")
    assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
    assert store.rows("SELECT status FROM inventory") == [{"status": "QUARANTINED"}]
    assert store.claim() is None
