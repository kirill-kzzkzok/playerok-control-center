"""Unrelated chat degradation is not account or paid-order authority loss."""

import json

import httpx
import pytest

from app.gifts.worker import GiftWorker
from app.playerok.adapter import APIError, Playerok, SafeAccount
from app.playerok.thread_calls import close_calls
from app.services.orders import Orders
from app.services.playerok_connection import PlayerokConnection
from app.services.product_readiness import ProductReadiness
from app.workers.sync import Synchronizer
from tests.test_stability_deal_completeness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui


@pytest.fixture
async def shop(tmp_path):
    async for create in shop_fixture.__wrapped__(tmp_path):
        yield create


async def test_old_chat_error_does_not_block_confirmed_new_gift_sale(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_chats":
            return r.api.page("chats", [{"id": "old-chat"}])
        if method == "get_chat_messages":
            raise APIError("HTTP_503")
        return await original(method, **kwargs)

    r.api.call = call
    await Synchronizer(r.store, r.api, Orders(r.store)).once()
    health = r.store.setting("sync:health")
    assert health["status"] == "ERROR" and health["errors"] == {"messages:old-chat": "HTTP_503"}
    readiness = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    assert "SYNC_ERROR" not in {p["code"] for p in readiness["problems"]}
    Orders(r.store).ingest(r.api.order("new-paid"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    assert len(r.api.remote.payments) == 1
    assert r.store.setting("sync:health")["status"] == "ERROR"


@pytest.mark.parametrize("status", [503, 401])
async def test_real_http_read_failure_distinguishes_degradation_and_auth_loss(tmp_path, status):
    from app.database.store import Store

    failed = False

    def respond(request):
        data = json.loads(request.content) if request.method == "POST" else dict(request.url.params)
        if failed:
            return httpx.Response(status, json={"message": "test-only failure"})
        if data["operationName"] == "viewer":
            return httpx.Response(
                200, json={"data": {"viewer": {"id": "seller", "username": "shop", "isBlocked": False}}}
            )
        if data["operationName"] == "user":
            return httpx.Response(
                200,
                json={
                    "data": {"user": {"__typename": "User", "id": "seller", "profile": {"username": "shop"}}}
                },
            )
        raise AssertionError("Unexpected write or read")

    store = Store(tmp_path / "isolated.sqlite")
    account = SafeAccount(
        "token=test-only", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    api = Playerok(account)
    connection = PlayerokConnection(store, api)
    try:
        assert (await connection.check())["status"] == "CONNECTED"
        failed = True
        with pytest.raises(APIError):
            await api.call("get_item", id="item")
        assert account.connection_verified is (status == 503)
        assert account.writes_enabled is (status == 503)
        if status == 401:
            assert connection.state["status"] == "DISCONNECTED"
    finally:
        await close_calls(api)
        account.close()
        store.close()


@pytest.mark.parametrize("errors", [{"orders": "HTTP_503"}, {"other-stream": "HTTP_503"}, None])
async def test_order_or_unclassified_sync_failure_still_blocks_gift_admission(shop, errors):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    r.store.set_setting("sync:health", {"status": "ERROR", "errors": errors})
    Orders(r.store).ingest(r.api.order("blocked"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    assert not r.store.rows("SELECT * FROM gift_jobs")
    assert not r.api.remote.payments
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    assert "SYNC_ERROR" in {p["code"] for p in result["problems"]}
