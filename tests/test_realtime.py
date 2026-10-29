import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from app.database.store import Store
from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.workers.realtime import TOPICS, Realtime
from app.workers.sync import Synchronizer


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "realtime.sqlite")
    api = SimpleNamespace(
        account=SimpleNamespace(id="seller", cookies={"token": "private"}, user_agent="Test"),
        call=AsyncMock(),
    )
    sync = Synchronizer(store, api, Orders(store), pages_per_stream=1)
    yield store, api, sync, Realtime(store, api.account, sync)
    store.close()


class Socket:
    def __init__(self, packets):
        self.packets, self.sent = iter(packets), []

    async def receive(self, timeout):
        try:
            value = next(self.packets)
        except StopIteration:
            await asyncio.sleep(timeout)
            raise TimeoutError from None
        if value.get("id") in TOPICS:
            ident = next(
                packet["id"]
                for packet in self.sent
                if packet.get("type") == "subscribe" and packet["payload"]["operationName"] == value["id"]
            )
            value = {**value, "id": ident}
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(value))

    async def send_json(self, packet):
        self.sent.append(packet)


async def test_ack_scopes_subscriptions_and_persists_coalesced_hints_before_api(setup):
    store, api, _sync, realtime = setup
    event = {
        "id": "chatUpdated",
        "type": "next",
        "payload": {
            "data": {
                "chatUpdated": {
                    "id": "chat",
                    "lastMessage": {"deal": {"id": "deal"}, "text": "private buyer message"},
                }
            }
        },
    }
    socket = Socket(
        [
            {"type": "connection_ack"},
            {"type": "ping", "payload": {"id": 1}},
            event,
            event,
            {"type": "complete"},
        ]
    )
    with pytest.raises(APIError, match="TERMINATED"):
        await realtime.serve(socket)
    assert socket.sent[0]["type"] == "connection_init"
    subscriptions = [packet for packet in socket.sent if packet["type"] == "subscribe"]
    assert [packet["payload"]["operationName"] for packet in subscriptions] == list(TOPICS)
    assert all("subscription " in packet["payload"]["query"] for packet in subscriptions)
    assert subscriptions[0]["payload"]["variables"]["filter"]["userId"] == "seller"
    assert socket.sent[-1] == {"type": "pong", "payload": {"id": 1}}
    assert store.rows("SELECT revision FROM sync_requests WHERE key='chat:chat'") == [{"revision": 2}]
    assert "private buyer message" not in str(store.rows("SELECT * FROM sync_requests"))
    api.call.assert_not_called()


async def test_missing_pong_ends_connection(setup):
    _, _, _, realtime = setup
    realtime.idle = realtime.pong_timeout = 0.001
    socket = Socket([{"type": "connection_ack"}])
    with pytest.raises(APIError, match="PONG_TIMEOUT"):
        await realtime.serve(socket)
    assert socket.sent[-1] == {"type": "ping"}


async def test_native_transport_activity_cannot_extend_application_ping_deadline(setup):
    _, _, _, realtime = setup
    realtime.idle = realtime.pong_timeout = 0.001

    class NativePings(Socket):
        async def receive(self, timeout):
            if not self.sent or len(self.sent) == 1:
                return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data='{"type":"connection_ack"}')
            # Simulates a transport consuming native pings without returning TEXT.
            await asyncio.sleep(10)

    socket = NativePings([])
    with pytest.raises(APIError, match="PONG_TIMEOUT"):
        await asyncio.wait_for(realtime.serve(socket), 1)


@pytest.mark.parametrize(
    "packet",
    [
        {"type": "next", "id": "unknown", "payload": {}},
        {"type": "next", "id": "userUpdated", "payload": {"data": {"userUpdated": {"id": "other"}}}},
        {"type": "next", "id": "chatUpdated", "payload": {"data": {"chatUpdated": {}}}},
    ],
)
def test_invalid_or_foreign_packets_cannot_queue_requests(setup, packet):
    store, _, _, realtime = setup
    with pytest.raises(APIError):
        realtime.signal(packet)
    assert not store.rows("SELECT * FROM sync_requests")


async def test_stop_cancels_active_receive_and_closes_connector(setup):
    store, _, _, realtime = setup
    closed = asyncio.Event()

    @asynccontextmanager
    async def connector():
        try:
            yield Socket([{"type": "connection_ack"}])
        finally:
            closed.set()

    realtime.connector = connector
    stop = asyncio.Event()
    task = asyncio.create_task(realtime.run(stop))
    for _ in range(50):
        if store.setting("realtime:health", {}).get("status") == "CONNECTED":
            break
        await asyncio.sleep(0.001)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert closed.is_set()
    assert store.setting("realtime:health")["status"] == "STOPPED"


async def test_new_revision_during_api_read_is_not_lost_and_restart_replays_safely(setup):
    store, api, sync, _ = setup
    sync.request("chat", "chat")

    async def call(method, **kwargs):
        if method == "get_chat":
            sync.request("chat", "chat")
            return {"id": "chat"}
        return {"messages": [{"id": "message", "text": "hello"}], "page_info": {"has_next_page": False}}

    api.call.side_effect = call
    await sync.process_requests()
    row = store.rows("SELECT * FROM sync_requests")[0]
    assert row["revision"] == 2 and row["completed_revision"] == 1
    api.call.side_effect = lambda method, **kwargs: (
        {"id": "chat"}
        if method == "get_chat"
        else {"messages": [{"id": "message"}], "page_info": {"has_next_page": False}}
    )
    await Synchronizer(store, api, Orders(store)).process_requests()
    assert store.rows("SELECT revision=completed_revision AS done FROM sync_requests")[0]["done"]
    assert len(store.rows("SELECT * FROM inbox")) == 1


async def test_failed_request_backoff_does_not_block_other_targets(setup):
    store, api, sync, _ = setup
    sync.request("chat", "bad")
    sync.request("deal", "sale")

    async def call(method, **kwargs):
        if method == "get_chat":
            raise APIError("HTTP_403")
        return {"id": "sale", "direction": "OUT", "status": "PAID", "item": {"id": "item", "price": 100}}

    api.call.side_effect = call
    await sync.process_requests()
    before = api.call.await_count
    await sync.process_requests()
    assert api.call.await_count == before
    assert store.rows("SELECT error_code,failures FROM sync_requests WHERE target='bad'") == [
        {"error_code": "HTTP_403", "failures": 1}
    ]
    assert len(store.rows("SELECT * FROM orders")) == 1


async def test_actual_aiohttp_protocol_and_redirect_guard(setup, monkeypatch):
    _, _, _, realtime = setup
    other_called = False

    async def handler(request):
        ws = web.WebSocketResponse(protocols=("graphql-transport-ws",))
        await ws.prepare(request)
        assert request.headers["cookie"] == "token=private"
        assert (await ws.receive_json())["type"] == "connection_init"
        await ws.send_json({"type": "connection_ack"})
        for _ in TOPICS:
            assert (await ws.receive_json())["type"] == "subscribe"
        await ws.close()
        return ws

    async def redirect(request):
        raise web.HTTPFound("/other")

    async def other(request):
        nonlocal other_called
        other_called = True
        return web.Response()

    app = web.Application()
    app.router.add_get("/graphql", handler)
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/other", other)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        monkeypatch.setattr("app.workers.realtime.WS_URL", f"ws://127.0.0.1:{port}/graphql")
        with pytest.raises(APIError, match="CLOSED"):
            await realtime.connection()
        monkeypatch.setattr("app.workers.realtime.WS_URL", f"ws://127.0.0.1:{port}/redirect")
        with pytest.raises(APIError, match="ENDPOINT_FORBIDDEN"):
            await realtime.connection()
        assert not other_called
    finally:
        await runner.cleanup()


async def test_transport_failure_reconnects_and_keeps_api_catchup_durable(setup, monkeypatch):
    store, _, _, realtime = setup
    stop, attempts = asyncio.Event(), []
    monkeypatch.setattr("app.workers.realtime.random.uniform", lambda *args: 0)

    @asynccontextmanager
    async def failing():
        attempts.append(1)
        if len(attempts) == 2:
            stop.set()
        raise OSError("private response must not be logged")
        yield  # pragma: no cover -- context manager with failing setup

    realtime.connector = failing
    await asyncio.wait_for(realtime.run(stop), 5)
    assert len(attempts) == 2
    assert store.rows("SELECT kind FROM sync_requests") == [{"kind": "catchup"}]
    assert "private response" not in str(store.rows("SELECT * FROM settings"))


async def test_hint_survives_database_close_and_reopen(tmp_path):
    path = tmp_path / "restart.sqlite"
    store = Store(path)
    Synchronizer(store, None, Orders(store)).request("deal", "sale")
    store.close()
    store = Store(path)
    try:
        api = SimpleNamespace(
            call=AsyncMock(
                return_value={"id": "sale", "direction": "OUT", "status": "PAID", "item": {"id": "item"}}
            )
        )
        await Synchronizer(store, api, Orders(store)).process_requests()
        assert len(store.rows("SELECT * FROM orders")) == 1
        assert store.rows("SELECT revision=completed_revision AS done FROM sync_requests")[0]["done"]
    finally:
        store.close()


async def test_incomplete_deal_uses_backoff_without_starving_chat(setup):
    store, api, sync, _ = setup
    sync.request("deal", "bad")
    sync.request("chat", "good")

    async def call(method, **kwargs):
        if method == "get_deal":
            return {"id": "bad", "direction": "OUT", "status": "PAID", "item": {}}
        if method == "get_chat":
            return {"id": "good"}
        return {"messages": [], "page_info": {"has_next_page": False}}

    api.call.side_effect = call
    await sync.process_requests()
    assert (
        store.rows("SELECT error_code FROM sync_requests WHERE target='bad'")[0]["error_code"]
        == "SYNC_REQUEST_SCHEMA_ERROR"
    )
    assert store.rows("SELECT revision=completed_revision AS done FROM sync_requests WHERE target='good'")[0][
        "done"
    ]
