import asyncio
import threading

import httpx
import pytest

from app.database.store import Store
from app.playerok.adapter import APIError, Playerok, SafeAccount
from app.playerok.thread_calls import close_calls
from app.services.playerok_connection import PlayerokConnection
from tests.test_product_readiness import shop as shop_fixture
from tests.test_thread_calls import entered

shop = shop_fixture


async def test_periodic_profile_wait_allows_verified_reads_but_never_mutations(tmp_path):
    store = Store(tmp_path / "connection.sqlite")
    account = SafeAccount(
        "token=fake",
        writes_enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))),
    )
    account.id = "seller"
    account.automation_enabled = True
    started, release = threading.Event(), threading.Event()
    slow = False

    def viewer():
        if slow:
            started.set()
            assert release.wait(5)
            raise APIError("NETWORK_ERROR")
        return account

    account.get = viewer
    account.get_deals = lambda **kwargs: {"deals": [{"id": "paid"}]}
    api = Playerok(account)
    connection = PlayerokConnection(store, api)
    try:
        assert (await connection.check(cooldown=0))["status"] == "CONNECTED"
        slow = True
        check = asyncio.create_task(connection.check(cooldown=0))
        await entered(started)
        try:
            assert not account.writes_enabled and not account.connection_verified
            assert (await api.call("get_deals"))["deals"] == [{"id": "paid"}]
        finally:
            release.set()
            await check
        assert (await api.call("get_deals"))["deals"]
        assert not account.writes_enabled
        slow = False
        assert (await connection.check(cooldown=0))["status"] == "CONNECTED"
        assert account.writes_enabled and account.automation_enabled
        connection.invalidate("AUTH_REJECTED")
        with pytest.raises(APIError, match="CONNECTION_NOT_VERIFIED"):
            await api.call("get_deals")
    finally:
        release.set()
        await close_calls(api)
        account.close()
        store.close()


@pytest.mark.parametrize("safe_account", [False, True])
@pytest.mark.parametrize("mode", ["unique_stock", "fixed_text"])
async def test_profile_recheck_between_reservation_and_dispatch_waits_without_losing_order(
    shop, mode, safe_account
):
    from app.services.orders import Orders
    from app.services.playerok_connection import PlayerokConnection
    from app.workers.delivery_chains import ChainWorker
    from app.workers.enrollment import EnrollmentWorker
    from tests.test_universal_delivery_e2e import setup_ui

    r = await shop()
    await setup_ui(r, mode, "reserved-content")
    Orders(r.store).ingest(r.api.order("paid"))
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    if safe_account:
        account = SafeAccount("token=fake", writes_enabled=True)
        account.id = "seller"
        account.automation_enabled = True
        r.api.account = account
    connection = PlayerokConnection(r.store, r.api)
    r.store.set_setting("playerok:bound_account_id", "seller")
    connection.read_account_id = "seller"
    # The order was already claimed before the periodic profile probe closed writes.
    r.api.account.writes_enabled = r.api.account.automation_enabled = True
    original = r.api.call

    async def call(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_chat":
            connection.invalidate("NETWORK_ERROR")
        return result

    r.api.call = call
    await r.worker.once()
    action = r.store.rows("SELECT * FROM outbox WHERE kind IN ('delivery','delivery_text')")[0]
    assert action["status"] == "RETRYABLE_FAILED"
    assert action["started_at"] is None
    assert not r.api.messages
    if mode == "unique_stock":
        assert r.store.rows(
            "SELECT i.status FROM inventory i JOIN reservations r ON r.inventory_id=i.id WHERE r.order_id='paid'"
        ) == [{"status": "RESERVED"}]
    if safe_account:
        account.close()
        return  # Exercise the real permit refusal; recovery+one-send is checked above without paid transport.
    r.api.call = original
    # The simulated boundary returns the next successful viewer verification.
    r.api.account.writes_enabled = r.api.account.automation_enabled = True
    r.api.account.connection_verified = True
    connection.state.update(status="CONNECTED", reason=None)
    r.store.execute("UPDATE outbox SET retry_at=0")
    await r.worker.once()
    await r.worker.once()
    assert (
        sum(
            m["text"] == ("isolated-unit-one" if mode == "unique_stock" else "reserved-content")
            for m in r.api.messages
        )
        == 1
    )


async def test_profile_probe_mid_admission_does_not_block_the_next_paid_order(shop):
    from app.services.orders import Orders
    from app.workers.delivery_chains import ChainWorker
    from app.workers.enrollment import EnrollmentWorker
    from tests.test_universal_delivery_e2e import setup_ui

    r = await shop()
    await setup_ui(r, "fixed_text", "admitted-text")
    for ident in ("paid-a", "paid-b"):
        Orders(r.store).ingest(r.api.order(ident))
    connection = PlayerokConnection(r.store, r.api)
    r.store.set_setting("playerok:bound_account_id", "seller")
    connection.read_account_id = "seller"
    r.api.account.writes_enabled = r.api.account.automation_enabled = True
    original = r.api.call

    async def call(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_deal":
            connection.invalidate("NETWORK_ERROR")
        return result

    r.api.call = call
    await EnrollmentWorker(r.store, r.api).once()
    assert not r.store.rows("SELECT * FROM auto_enrollment WHERE status='BLOCKED'")
    assert not r.api.messages
    r.api.call = original
    r.api.account.writes_enabled = r.api.account.automation_enabled = True
    connection.state.update(status="CONNECTED", reason=None)
    await EnrollmentWorker(r.store, r.api).once()
    for _ in range(6):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert sum(m["text"] == "admitted-text" for m in r.api.messages) == 2
