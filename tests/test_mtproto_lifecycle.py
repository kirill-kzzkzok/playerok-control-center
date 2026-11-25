import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from telethon import TelegramClient, types
from telethon.sessions import MemorySession

from app.gifts.mtproto import GiftFailure, PersonalGiftSender
from app.gifts.settings import GiftSettings


class Transport:
    def __init__(self):
        self.connecting = asyncio.Event()
        self.connected = False
        self.handshake = asyncio.Event()
        self.disconnecting = asyncio.Event()
        self.drain = asyncio.Event()
        self.drain.set()
        self.loops = []

    async def connect(self):
        assert all(task.done() for task in self.loops)
        self.loops = [asyncio.create_task(asyncio.Event().wait()) for _ in range(2)]
        self.connecting.set()
        await self.handshake.wait()
        self.connected = True

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return types.User(id=7)

    async def disconnect(self):
        self.disconnecting.set()
        await self.drain.wait()
        for task in self.loops:
            task.cancel()
        await asyncio.gather(*self.loops, return_exceptions=True)
        self.connected = False


@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = GiftSettings(
        1, "fake", str(tmp_path / "fake.session"), "", "", "", False, 1, 1, telegram_transport="direct"
    )
    clients = []

    def factory(*args, **kwargs):
        client = Transport()
        clients.append(client)
        if len(clients) > 1:
            client.handshake.set()
        return client

    monkeypatch.setattr("app.gifts.mtproto.TelegramClient", factory)
    proxy = SimpleNamespace(ready=True, state={"generation": 1})

    def check():
        if not proxy.ready:
            raise OSError("System tunnel unavailable")
        return proxy.state["generation"]

    proxy.check = check
    monkeypatch.setattr("app.gifts.mtproto.HappRoute", lambda: proxy)
    monkeypatch.setattr("app.gifts.mtproto.happ_required", lambda mode: mode == "direct")
    yield PersonalGiftSender(settings), proxy, clients


async def test_cancelled_handshake_drains_telethon_transport_tasks(setup, monkeypatch):
    sender, _, _ = setup
    handshake, peer_closed = asyncio.Event(), asyncio.Event()

    async def peer(reader, writer):
        try:
            await reader.read(1)
            handshake.set()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            peer_closed.set()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    session = MemorySession()
    session.set_dc(2, "127.0.0.1", server.sockets[0].getsockname()[1])
    client = TelegramClient(session, 1, "fake", connection_retries=0, auto_reconnect=False)
    monkeypatch.setattr("app.gifts.mtproto.TelegramClient", lambda *a, **k: client)
    async with server:
        opening = asyncio.create_task(sender.connect())
        try:
            await asyncio.wait_for(handshake.wait(), 3)
            transport = client._sender._connection
            loops = (transport._send_task, transport._recv_task)
            assert all(task is not None and not task.done() for task in loops)
            opening.cancel()
            with pytest.raises(asyncio.CancelledError):
                await opening
            assert all(task.done() for task in loops)
            await asyncio.wait_for(peer_closed.wait(), 3)
        finally:
            opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)
            await sender.close()


async def test_close_waits_for_disconnection_even_if_caller_cancelled(setup):
    sender, _, clients = setup
    opening = asyncio.create_task(sender.connect())
    await asyncio.sleep(0)
    client = clients[0]
    client.handshake.set()
    await opening
    client.drain.clear()
    closing = asyncio.create_task(sender.close())
    try:
        await client.disconnecting.wait()
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done() and sender.session_lock is not None
    finally:
        client.drain.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await sender.close()
    assert sender.session_lock is None and all(task.done() for task in client.loops)


async def test_route_change_replaces_transport_after_disconnection(setup):
    sender, proxy, clients = setup
    opening = asyncio.create_task(sender.connect())
    await asyncio.sleep(0)
    old = clients[0]
    old.handshake.set()
    await opening
    old_connection = sender.connection
    try:
        proxy.ready = False
        with pytest.raises(GiftFailure, match="PROXY_UNAVAILABLE"):
            await sender.connect()
        proxy.state["generation"] = 2
        proxy.ready = True
        await asyncio.gather(sender.connect(), sender.connect())
        assert len(clients) == 2 and all(task.done() for task in old.loops)
        sender.settings = replace(sender.settings, payment_enabled=True)
        with pytest.raises(GiftFailure, match="PROXY_ROUTE_CHANGED"):
            await sender.pay(SimpleNamespace(connection=old_connection))
    finally:
        await sender.close()


async def test_node_change_during_handshake_never_publishes_stale_client(setup):
    sender, proxy, clients = setup
    opening = asyncio.create_task(sender.connect())
    await asyncio.sleep(0)
    old = clients[0]
    try:
        proxy.state["generation"] = 2
        old.handshake.set()
        with pytest.raises(GiftFailure, match="PROXY_ROUTE_CHANGED"):
            await opening
        assert sender.me is None and all(task.done() for task in old.loops)
        await sender.connect()
        assert sender.route_generation == 2 and len(clients) == 2
    finally:
        await sender.close()


@pytest.mark.parametrize("paid", [False, True])
async def test_half_open_rpc_is_drained_before_next_connection(tmp_path, monkeypatch, paid):
    from telethon import functions

    clients = []
    entered = asyncio.Event()

    class Client(Transport):
        def __init__(self):
            super().__init__()
            self.handshake.set()
            self.payments = 0

        async def __call__(self, request):
            if isinstance(request, functions.payments.SendStarsFormRequest):
                self.payments += 1
                raise ConnectionError("reply lost after possible payment")
            entered.set()
            if len(clients) == 1:
                await asyncio.Event().wait()  # half-open TCP, still is_connected=True
            return SimpleNamespace(balance=types.StarsAmount(135, 0))

    def factory(*args, **kwargs):
        client = Client()
        clients.append(client)
        return client

    monkeypatch.setattr("app.gifts.mtproto.TelegramClient", factory)
    sender = PersonalGiftSender(
        GiftSettings(1, "fake", str(tmp_path / "half-open.session"), "", "", "", True, 15, 300)
    )
    task = None
    try:
        if paid:
            await sender.connect()
            prepared = SimpleNamespace(connection=sender.connection, form_id=1, invoice=object())
            with pytest.raises(ConnectionError):
                await sender.pay(prepared)
        else:
            task = asyncio.create_task(sender.balance())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        old = clients[0]
        assert sender.me is None and sender.client is None
        assert all(task.done() for task in old.loops)
        if paid:
            with pytest.raises(GiftFailure, match="SESSION_CLOSED"):
                await sender.pay(prepared)
            assert old.payments == 1
        assert await sender.balance() == 135
        assert len(clients) == 2
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await sender.close()


async def test_late_failure_of_old_read_cannot_disconnect_new_client(tmp_path, monkeypatch):
    clients = []
    entered, release = asyncio.Event(), asyncio.Event()

    class Client(Transport):
        def __init__(self, old):
            super().__init__()
            self.old = old
            self.handshake.set()

        async def __call__(self, request):
            if self.old:
                entered.set()
                await release.wait()
                raise ConnectionError("late old connection failure")
            return SimpleNamespace(balance=types.StarsAmount(135, 0))

    def factory(*args, **kwargs):
        client = Client(not clients)
        clients.append(client)
        return client

    monkeypatch.setattr("app.gifts.mtproto.TelegramClient", factory)
    sender = PersonalGiftSender(
        GiftSettings(1, "fake", str(tmp_path / "late-read.session"), "", "", "", False, 1, 1)
    )
    old_read = asyncio.create_task(sender.balance())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        clients[0].connected = False
        await sender.connect()
        replacement = sender.client
        assert replacement is clients[1] and all(task.done() for task in clients[0].loops)
        release.set()
        with pytest.raises(ConnectionError):
            await old_read
        assert sender.client is replacement and sender.me is not None
        assert all(not task.done() for task in replacement.loops)
        assert await sender.balance() == 135
    finally:
        release.set()
        old_read.cancel()
        await asyncio.gather(old_read, return_exceptions=True)
        await sender.close()


async def test_idle_mtproto_disconnect_exception_is_observed_before_reconnect(setup):
    sender, _, clients = setup
    opening = asyncio.create_task(sender.connect())
    await asyncio.sleep(0)
    client = clients[0]
    observed = []

    class Disconnected(asyncio.Future):
        def exception(self):
            result = super().exception()
            observed.append(result)
            return result

    client.disconnected = Disconnected()
    client.handshake.set()
    await opening
    error = asyncio.IncompleteReadError(b"", 8)
    try:
        client.connected = False
        client.disconnected.set_exception(error)
        await asyncio.sleep(0)
        assert observed == [error]
        await sender.connect()
        assert sender.client is not client and sender.me.id == 7
        assert all(task.done() for task in client.loops)
    finally:
        if not observed and client.disconnected.done():
            client.disconnected.exception()
        await sender.close()
