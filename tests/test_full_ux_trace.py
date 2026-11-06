import logging
from time import perf_counter

import httpx

from app.playerok.adapter import Playerok, SafeAccount
from app.telemetry import finish, measurements
from tests.test_product_readiness import shop as shop_fixture

shop = shop_fixture


async def test_dispatcher_debug_trace_has_safe_action_and_no_callback_identifier(shop, caplog):
    r = await shop()
    caplog.set_level(logging.DEBUG, logger="app.telemetry")
    await r.ui.click("item:lot")
    traces = [record.args[-1] for record in caplog.records if record.name == "app.telemetry"]
    trace = traces[-1]
    assert trace["context"]["callback_group"] == "item"
    assert trace["context"]["action"] == "item"
    assert "lot" not in str(trace)
    assert all(stage in trace for stage in ("ack", "fsm", "navigation_lock"))


async def test_adapter_trace_counts_safe_retry_and_http_without_payload(caplog):
    caplog.set_level(logging.DEBUG, logger="app.telemetry")
    account = SafeAccount("token=DO_NOT_LOG_THIS", writes_enabled=False)
    account.id = "owner-private-id"
    account.client.close()
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        return httpx.Response(500 if count == 1 else 200, json={"data": {"item": {"id": "confirmed"}}})

    account.client = httpx.Client(transport=httpx.MockTransport(respond))
    api = Playerok(account)
    values = {}
    token = measurements.set(values)
    try:
        # Real adapter retries, real SafeAccount HTTP boundary; return a dictionary
        # only to isolate SDK shape parsing from this diagnostics assertion.
        def read(**kwargs):
            result = account.request(
                "get", "https://playerok.com/graphql", {}, payload={"operationName": "item"}
            )
            return result.json()["data"]["item"]

        account.get_item = read
        assert await api.call("get_item", id="private-item") == {"id": "confirmed"}
        assert values["playerok_methods"]["get_item"] == {"attempts": 2, "retries": 1}
        assert values["http"] >= 0 and count == 2
        finish(perf_counter() - 3.01, values)
        assert "DO_NOT_LOG_THIS" not in caplog.text and "private-item" not in caplog.text
    finally:
        measurements.reset(token)
        account.close()
