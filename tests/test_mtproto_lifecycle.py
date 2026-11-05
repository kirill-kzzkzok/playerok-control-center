import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from telethon import TelegramClient, types
from telethon.sessions import MemorySession

from app.gifts.mtproto import GiftFailure, PersonalGiftSender
from app.gifts.settings import GiftSettings
from app.localvpn import active_proxy


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
        1, "fake", str(tmp_path / "fake.session"), "", "", "", False, 1, 1, telegram_transport="localvpn"
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
    token = active_proxy.set(proxy)
    try:
        yield PersonalGiftSender(settings), proxy, clients
    finally:
        active_proxy.reset(token)


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
