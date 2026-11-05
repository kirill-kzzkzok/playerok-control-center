import asyncio
import threading

import httpx
import pytest

from app.playerok.adapter import APIError, Playerok, SafeAccount
from app.playerok.thread_calls import drain_calls
from tests.test_thread_calls import entered
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


async def test_title_input_does_not_load_unneeded_category_schema(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    r.api.account.auto_accept_agreements = True
    original = r.api.call

    async def call(method, **kwargs):
        if method.startswith("get_game_category"):
            raise APIError("HTTP_500")
        return await original(method, **kwargs)

    r.api.call = call
    await r.ui.click("editname:lot")
    assert "❗" not in r.session.last_panel.text
    assert "Введите новое значение" in r.session.last_panel.text


async def test_independent_safe_reads_do_not_wait_for_slow_history():
    started, release = threading.Event(), threading.Event()

    def respond(request):
        if request.url.params.get("id") == "slow":
            started.set()
            assert release.wait(5)
        return httpx.Response(200, json={"data": {"viewer": {"id": "seller"}, "probe": {"id": "ok"}}})

    account = SafeAccount(
        "token=test", writes_enabled=False, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    api = Playerok(account)
    account.get_chat_messages = lambda chat_id: account.request(
        "get",
        account.base_url + "/graphql",
        {},
        {"operationName": "viewer", "query": "query { viewer { id } }", "id": "slow"},
    ).json()["data"]["probe"]
    account.get_item = lambda id: account.request(
        "get",
        account.base_url + "/graphql",
        {},
        {"operationName": "viewer", "query": "query { viewer { id } }", "id": id},
    ).json()["data"]["probe"]
    slow = asyncio.create_task(api.call("get_chat_messages", chat_id="slow"))
    try:
        await entered(started)
        assert await asyncio.wait_for(api.call("get_item", id="fast"), 1) == {"id": "ok"}
    finally:
        release.set()
        await slow
        await drain_calls(api)
        account.close()


@pytest.mark.parametrize("method", ["get_item", "get_chat_messages", "get_game_category_obtaining_types"])
async def test_transient_read_failure_retries_once_without_mutation(method):
    calls = []
    account = SafeAccount("token=test", writes_enabled=False)
    api = Playerok(account)

    def read(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise APIError("HTTP_500")
        return {"id": "confirmed"}

    setattr(account, method, read)
    try:
        assert await api.call(method) == {"id": "confirmed"}
        assert len(calls) == 2
    finally:
        account.close()
