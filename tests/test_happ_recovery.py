"""System tunnel admission uses existing recovery, never the default adapter."""

import socket
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import SendMessage

from app.gifts.mtproto import GiftFailure, PersonalGiftSender
from app.gifts.settings import GiftSettings
from app.telegram.errors import TelegramRequestNotStarted
from app.telegram.transport import TelegramNetworkSession
from tests.test_mtproto_lifecycle import Transport


class Tunnel:
    def __init__(self):
        self.up = False
        self.generation = 1

    def check(self):
        if not self.up:
            raise OSError("Happ tunnel unavailable")
        return self.generation


@pytest.mark.parametrize("outage_seconds", [10, 60, 300])
async def test_bot_waits_without_http_then_resumes_and_survives_another_outage(monkeypatch, outage_seconds):
    import asyncio

    from aiogram.types import User, WebhookInfo

    from app import __main__ as application

    tunnel = Tunnel()
    session = TelegramNetworkSession()
    session.happ_guard = tunnel
    bot = Bot("123:" + "x" * 35, session=session)
    requests, delays, clock = [], [], [0.0]
    restored_at = [float(outage_seconds)]

    async def boundary(self, bot, method, timeout=None):
        requests.append(method.__api_method__)
        if method.__api_method__ == "getMe":
            return User(id=123, is_bot=True, first_name="test")
        if method.__api_method__ == "getWebhookInfo":
            return WebhookInfo(url="", has_custom_certificate=False, pending_update_count=0)
        return True

    async def timed_wait(coro, delay):
        coro.close()
        assert 0 < delay <= 30
        delays.append(delay)
        clock[0] += delay
        tunnel.up = clock[0] >= restored_at[0]
        raise TimeoutError

    monkeypatch.setattr(AiohttpSession, "make_request", boundary)
    monkeypatch.setattr(application, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(application, "asyncio", SimpleNamespace(wait_for=timed_wait, timeout=asyncio.timeout))
    try:
        with pytest.raises(TelegramRequestNotStarted):
            await session.make_request(bot, SendMessage(chat_id=7, text="notice"))
        assert requests == []
        assert await application.wait_for_telegram(bot, asyncio.Event())
        assert outage_seconds <= clock[0] <= outage_seconds + 30
        assert delays and len(delays) < outage_seconds + 2
        assert await session.make_request(bot, SendMessage(chat_id=7, text="notice"))
        # A second outage immediately after recovery uses the same production backoff.
        tunnel.up = False
        restored_at[0] = clock[0] + outage_seconds
        with pytest.raises(TelegramRequestNotStarted):
            await session.make_request(bot, SendMessage(chat_id=7, text="next notice"))
        tunnel.generation = 2
        assert await application.wait_for_telegram(bot, asyncio.Event())
        assert restored_at[0] <= clock[0] <= restored_at[0] + 30
        assert await session.make_request(bot, SendMessage(chat_id=7, text="next notice"))
        assert requests.count("sendMessage") == 2
    finally:
        await session.close()


async def test_telethon_drops_old_connection_before_recovery_and_rejects_stale_payment(tmp_path):
    tunnel = Tunnel()
    client = Transport()
    client.handshake.set()
    sender = PersonalGiftSender(
        GiftSettings(
            1, "fake", str(tmp_path / "fake.session"), "", "", "", True, 15, 300, telegram_transport="direct"
        ),
        client=client,
    )
    sender.happ_guard = tunnel
    try:
        with pytest.raises(GiftFailure, match="PROXY_UNAVAILABLE"):
            await sender.connect()
        assert not client.loops
        tunnel.up = True
        await sender.connect()
        old_connection = sender.connection
        tunnel.up = False
        with pytest.raises(GiftFailure, match="PROXY_UNAVAILABLE"):
            await sender.connect()
        assert sender.me is None and all(task.done() for task in client.loops)
        tunnel.up = True
        tunnel.generation = 2
        await sender.connect()
        assert sender.connection is not old_connection and sender.route_generation == 2
        prepared = SimpleNamespace(connection=old_connection)
        with pytest.raises(GiftFailure, match="PROXY_ROUTE_CHANGED"):
            await sender.pay(prepared)
    finally:
        await sender.close()


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_socket_is_pinned_and_never_falls_back_when_pinning_fails(monkeypatch, family):
    import socket

    from app.telegram import system_tunnel as module

    calls = []

    class Socket:
        def setsockopt(self, *args):
            calls.append(args)
            if failed:
                raise OSError("adapter gone during socket creation")

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(module, "interface", lambda: (52, 7))
    monkeypatch.setattr(module.socket, "socket", lambda *args: Socket())
    failed = False
    module.HappRoute().socket((family, socket.SOCK_STREAM, socket.IPPROTO_TCP))
    level = socket.IPPROTO_IP if family == socket.AF_INET else socket.IPPROTO_IPV6
    value = socket.htonl(52) if family == socket.AF_INET else 52
    assert calls == [(level, 31, value)]
    failed = True
    with pytest.raises(OSError):
        module.HappRoute().socket((family, socket.SOCK_STREAM, socket.IPPROTO_TCP))
    assert calls[-1] == "closed"


async def test_real_happ_connection_cancel_drains_telethon_loops_on_local_peer(tmp_path, monkeypatch):
    import asyncio
    import socket

    from telethon import TelegramClient
    from telethon.sessions import MemorySession

    from app.telegram.system_tunnel import HappConnectionTcpFull, HappRoute

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

    # Only the socket pin boundary is simulated; actual production Telethon codec,
    # TCP send/receive tasks, cancellation and disconnect run against a local peer.
    sockets = []

    def factory(self, info):
        sock = socket.socket(*info)
        sockets.append(sock)
        return sock

    monkeypatch.setattr(HappRoute, "socket", factory)
    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    session = MemorySession()
    session.set_dc(2, "127.0.0.1", server.sockets[0].getsockname()[1])
    client = TelegramClient(
        session, 1, "fake", connection=HappConnectionTcpFull, connection_retries=0, auto_reconnect=False
    )
    sender = PersonalGiftSender(
        GiftSettings(1, "fake", str(tmp_path / "local.session"), "", "", "", False, 1, 1), client=client
    )
    async with server:
        opening = asyncio.create_task(sender.connect())
        try:
            await asyncio.wait_for(handshake.wait(), 3)
            connection = client._sender._connection
            loops = (connection._send_task, connection._recv_task)
            assert sockets and all(task is not None and not task.done() for task in loops)
            opening.cancel()
            with pytest.raises(asyncio.CancelledError):
                await opening
            assert all(task.done() for task in loops)
            await asyncio.wait_for(peer_closed.wait(), 3)
        finally:
            opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)
            await sender.close()


async def test_personal_login_uses_the_same_pinned_connection(tmp_path, monkeypatch):

    from app.gifts import auth
    from app.telegram import system_tunnel

    settings = GiftSettings(
        1, "fake", str(tmp_path / "login.session"), "", "", "", False, 1, 1, telegram_transport="direct"
    )
    monkeypatch.setattr(auth.GiftSettings, "load", lambda: settings)
    monkeypatch.setattr(system_tunnel, "happ_required", lambda mode: mode == "direct")
    arguments = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_me(self):
            return SimpleNamespace(bot=False)

    def client(*args, **kwargs):
        arguments.append(kwargs)
        return Client()

    monkeypatch.setattr("telethon.TelegramClient", client)
    await auth.main()
    assert arguments[0].get("connection") is system_tunnel.HappConnectionTcpFull
    assert arguments[0]["auto_reconnect"] is False


@pytest.mark.parametrize("stalled_method", ["getMe", "getWebhookInfo"])
async def test_startup_probe_recovers_from_stalled_bot_api_read(monkeypatch, stalled_method):
    import asyncio

    from aiogram.types import User, WebhookInfo

    from app import __main__ as application

    bot = Bot("123:" + "x" * 35, session=TelegramNetworkSession())
    calls = []

    async def boundary(self, bot, method, timeout=None):
        name = method.__api_method__
        calls.append(name)
        if name == stalled_method and calls.count(name) == 1:
            await asyncio.Future()
        if name == "getMe":
            return User(id=123, is_bot=True, first_name="test")
        return WebhookInfo(url="", has_custom_certificate=False, pending_update_count=0)

    async def elapse_backoff(waiter, delay):
        waiter.close()
        raise TimeoutError

    monkeypatch.setattr(AiohttpSession, "make_request", boundary)
    monkeypatch.setattr(
        application,
        "asyncio",
        SimpleNamespace(timeout=lambda _: asyncio.timeout(0.01), wait_for=elapse_backoff),
    )
    try:
        async with asyncio.timeout(0.5):
            assert await application.wait_for_telegram(bot, asyncio.Event())
        assert calls.count(stalled_method) == 2
        assert calls[-1] == "getWebhookInfo"
    finally:
        await bot.session.close()
