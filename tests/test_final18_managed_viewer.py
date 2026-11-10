import json
import logging

import httpx

from app.database.store import Store
from app.playerok.adapter import APIError, Playerok, SafeAccount
from app.playerok.thread_calls import close_calls
from app.services.playerok_connection import PlayerokConnection


async def test_startup_viewer_uses_native_http_client_identity(tmp_path):
    store = Store(tmp_path / "startup-viewer.sqlite")
    seen = []

    def respond(request):
        payload = json.loads(request.content) if request.method == "POST" else request.url.params
        operation = payload["operationName"]
        seen.append(operation)
        if request.headers.get("user-agent") != client.headers["user-agent"]:
            return httpx.Response(
                403, text="<html>Verification required</html>", headers={"server": "ddos-guard"}
            )
        assert request.headers["cookie"] == "token=mock-only"
        if operation == "viewer":
            return httpx.Response(200, json={"data": {"viewer": {"id": "seller", "username": "shop"}}})
        assert operation == "user"
        return httpx.Response(
            200,
            json={"data": {"user": {"__typename": "User", "id": "seller", "profile": {"username": "shop"}}}},
        )

    client = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
    account = SafeAccount("token=mock-only", writes_enabled=True, client=client)
    api = Playerok(account)
    connection = PlayerokConnection(store, api)
    try:
        assert not account.writes_enabled
        assert (await connection.check(cooldown=0))["status"] == "CONNECTED"
        assert store.setting("playerok:bound_account_id") == "seller"
        assert account.writes_enabled and seen == ["viewer", "user"]
    finally:
        await close_calls(api)
        account.close()
        store.close()


async def test_managed_viewer_transport_failures_preserve_incident_accounting(tmp_path, caplog):
    store = Store(tmp_path / "viewer.sqlite")
    account = SafeAccount(
        "token=fake",
        writes_enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))),
    )
    account.id = "seller"
    account.automation_enabled = True
    api = Playerok(account)
    connection = PlayerokConnection(store, api)
    failure = None

    def viewer():
        if failure:
            raise APIError(failure)
        return account

    account.get = viewer
    try:
        with caplog.at_level(logging.DEBUG, logger="pcc.shop"):
            assert (await connection.check(cooldown=0))["status"] == "CONNECTED"
            caplog.clear()
            for failure in ("NETWORK_ERROR", "HTTP_503") * 5:
                assert (await connection.check(cooldown=0))["reason"] == failure
                assert connection.reads_allowed and not account.writes_enabled
            assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
            failure = None
            assert (await connection.check(cooldown=0))["status"] == "CONNECTED"
            assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1
    finally:
        await close_calls(api)
        account.close()
        store.close()
