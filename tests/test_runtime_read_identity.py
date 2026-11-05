import asyncio
import threading

import httpx
import pytest

from app.database.store import Store
from app.playerok.adapter import APIError, Playerok, SafeAccount
from app.playerok.thread_calls import close_calls
from app.services.playerok_connection import PlayerokConnection
from tests.test_thread_calls import entered


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
