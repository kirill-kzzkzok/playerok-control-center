import asyncio
import json
import logging
import threading
from copy import deepcopy
from time import perf_counter

import httpx
import pytest

from app.playerok.adapter import APIError, Playerok, SafeAccount
from app.playerok.thread_calls import drain_calls
from tests.test_final_outbox_resource_lanes import configure_other_item
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


async def test_fifty_lots_twenty_local_clicks_and_five_cards_during_slow_history(
    shop, caplog, record_property
):
    r = await shop()

    def update(id, **changes):
        r.api.mutations.append(deepcopy(changes))
        r.api.items[id].update(changes)
        return deepcopy(r.api.items[id])

    r.api.account.update_item = update
    for index in range(50):
        await configure_other_item(r, f"speed-{index:02}", "fixed_text")
    started, release = threading.Event(), threading.Event()
    account = SafeAccount("token=test", writes_enabled=False)
    account.id = "seller"
    transport = Playerok(account)
    account.get_item = lambda id: deepcopy(r.api.items[id])

    def history(**kwargs):
        started.set()
        assert release.wait(10)
        return {"messages": [], "page_info": {"has_next_page": False}}

    account.get_chat_messages = history
    original = r.api.call

    async def read(method, **kwargs):
        if method in {"get_item", "get_chat_messages"}:
            return await transport.call(method, **kwargs)
        return await original(method, **kwargs)

    r.api.call = read
    caplog.set_level(logging.DEBUG, logger="app.telemetry")
    waiting = asyncio.create_task(transport.call("get_chat_messages", chat_id="slow"))
    timings = []
    try:
        await entered(started)
        for _ in range(20):
            before = perf_counter()
            await r.ui.click("home")
            timings.append(perf_counter() - before)
        await asyncio.gather(
            *(r.ui.click(f"item:speed-{index:02}", message_id=1000 + index) for index in range(5))
        )
        assert not release.is_set()
    finally:
        release.set()
        await waiting
        await drain_calls(transport)
        account.close()
    stages = {
        stage: [] for stage in ("ack", "fsm", "navigation_lock", "playerok_lock", "playerok", "telegram")
    }
    for record in caplog.records:
        if record.name == "app.telemetry" and record.args and isinstance(record.args[-1], dict):
            for stage, values in stages.items():
                values.append(record.args[-1].get(stage, 0))

    def percentiles(values):
        ordered = sorted(values)
        return {
            str(p): ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p / 100))] for p in (50, 95, 99)
        }

    assert all(stages.values())
    record_property(
        "offline_ui_seconds",
        json.dumps(
            {
                "local": percentiles(timings),
                **{stage: percentiles(values) for stage, values in stages.items()},
            }
        ),
    )
    assert percentiles(timings)["95"] < 0.5
    assert not r.api.messages and not r.api.remote.payments
