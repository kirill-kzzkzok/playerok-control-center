import httpx
import pytest

from app.database.store import Store
from app.playerok.adapter import SafeAccount
from app.services.orders import Orders
from app.services.playerok_connection import PlayerokConnection
from app.services.products import Products
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.outbox import Executor, OutboxWorker
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_universal_delivery_e2e import simulation as simulation_fixture

simulation = simulation_fixture


@pytest.mark.parametrize("handler", ["fixed_text", "unique_stock"])
async def test_403_before_dispatch_preserves_paid_delivery_and_recovers_after_worker_restart(
    simulation, handler
):
    r = await simulation()
    await setup_ui(r, handler, "test-delivery" if handler == "fixed_text" else None)
    Orders(r.store).ingest(r.api.order("recover-paid"))
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    original_call = r.api.call
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"createChatMessage": {"id": "confirmed", "text": "test"}}})

    account = SafeAccount(
        "token=test-only", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    account.id = "seller"
    account.automation_enabled = True
    r.api.account = account

    async def call(method, **kwargs):
        if method == "get":
            return {"id": "seller", "profile": {}, "is_blocked": False}
        return await original_call(method, **kwargs)

    r.api.call = call
    connection = PlayerokConnection(r.store, r.api)
    assert (await connection.check(cooldown=0))["status"] == "CONNECTED"
    executor = Executor(r.store, r.api, Products(r.store, r.api))
    original_mutate = executor.mutate

    async def interrupted(action, operation, method, **kwargs):
        connection.invalidate("UNAUTHORIZED_OR_CHALLENGE", {"http_status": 403})
        return await original_mutate(action, operation, method, **kwargs)

    executor.mutate = interrupted
    try:
        worker = OutboxWorker(r.store, executor)
        assert await worker.once()
        row = r.store.rows("SELECT * FROM outbox WHERE target='recover-paid'")[0]
        assert row["status"] == "RETRYABLE_FAILED"
        assert row["started_at"] is None
        assert not requests
        if handler == "unique_stock":
            assert r.store.rows("SELECT status FROM inventory WHERE status='RESERVED'") == [
                {"status": "RESERVED"}
            ]
        assert (await connection.check(cooldown=0))["status"] == "CONNECTED"
        restarted = Store(r.store.path)
        try:
            restarted.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (row["id"],))
            worker = OutboxWorker(restarted, Executor(restarted, r.api, Products(restarted, r.api)))
            assert await worker.once()
            assert restarted.rows("SELECT status FROM outbox WHERE id=?", (row["id"],)) == [
                {"status": "SUCCEEDED"}
            ]
            assert len(requests) == 1
            assert not await worker.once()
        finally:
            restarted.close()
    finally:
        account.close()
