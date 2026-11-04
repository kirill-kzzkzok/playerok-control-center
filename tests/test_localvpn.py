from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError

from app.gifts.mtproto import GiftFailure, PersonalGiftSender, PreparedGift, Recipient
from app.gifts.settings import GiftSettings
from app.localvpn import (
    SOCKS_URL,
    NetworkSettings,
    PlayerokProxy,
    ProxyUnavailable,
    active_proxy,
    mtproto_proxy,
)
from app.telegram.transport import TelegramNetworkSession
from tests import test_gift_bundles as bundle_tests
from tests.test_gift_bundles import setup


@pytest.fixture
def rig(tmp_path):
    yield from bundle_tests.rig.__wrapped__(tmp_path)


def test_network_settings_default_and_explicit_legacy_modes(tmp_path):
    path = tmp_path / "network.env"
    path.write_text("", encoding="utf-8")
    assert NetworkSettings.load(path).mode == "localvpn"
    for mode in ("worker", "direct", "legacy"):
        path.write_text("TELEGRAM_TRANSPORT=" + mode, encoding="utf-8")
        assert NetworkSettings.load(path).mode == mode
    path.write_text("TELEGRAM_TRANSPORT=fall-back", encoding="utf-8")
    with pytest.raises(ValueError):
        NetworkSettings.load(path)
    assert mtproto_proxy("localvpn") == {
        "proxy_type": "socks5",
        "addr": "127.0.0.1",
        "port": 1080,
        "rdns": True,
    }


async def test_socks_bot_refuses_requests_without_owned_ready_service():
    session = TelegramNetworkSession(proxy=SOCKS_URL, localvpn=True)
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        with pytest.raises(TelegramNetworkError):
            await bot.get_me()
        assert session.results["getMe"]["http"] is None
        assert session.route.startswith("SOCKS5")
    finally:
        await session.close()


async def test_personal_sender_uses_same_socks_and_no_paid_retries(tmp_path, monkeypatch):
    from telethon import types

    s = GiftSettings(
        1,
        "private-hash",
        str(tmp_path / "test.session"),
        "category",
        "obtaining",
        "field",
        True,
        100,
        300,
        telegram_transport="localvpn",
    )
    client = SimpleNamespace(
        connect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=True),
        get_me=AsyncMock(return_value=types.User(id=7)),
        disconnect=AsyncMock(),
    )
    captured = {}

    def factory(*args, **kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setattr("app.gifts.mtproto.TelegramClient", factory)
    proxy = SimpleNamespace(ready=True, state={"generation": 1})
    token = active_proxy.set(proxy)
    sender = PersonalGiftSender(s)
    try:
        await sender.connect()
        assert captured["proxy"] == mtproto_proxy("localvpn")
        assert captured["request_retries"] == 0 and captured["auto_reconnect"] is False
        proxy.ready = False
        with pytest.raises(GiftFailure, match="PROXY_UNAVAILABLE"):
            await sender.connect()
        assert client.connect.await_count == 1
    finally:
        await sender.close()
        active_proxy.reset(token)


async def test_payment_route_change_never_dispatches_paid_rpc():
    s = GiftSettings(
        1, "hash", "data/test.session", "", "", "", True, 100, 300, telegram_transport="localvpn"
    )
    client = AsyncMock()
    sender = PersonalGiftSender(s, client=client)
    sender.me = SimpleNamespace(id=7)
    proxy = SimpleNamespace(ready=True, state={"generation": 2}, confirm=AsyncMock(return_value=2))
    token = active_proxy.set(proxy)
    try:
        with pytest.raises(GiftFailure, match="PROXY_ROUTE_CHANGED"):
            await sender.pay(PreparedGift(Recipient("@Receiver", 8, 9), 10, 15, 16, object(), 1))
        client.assert_not_awaited()
    finally:
        active_proxy.reset(token)


async def test_proxy_control_corrupt_reply_closes_pipe_and_admission():
    proxy = PlayerokProxy()
    stdin = SimpleNamespace(write=lambda b: None, drain=AsyncMock(), close=Mock())
    proxy.process = SimpleNamespace(
        returncode=None, stdin=stdin, stdout=SimpleNamespace(readline=AsyncMock(return_value=b"not json\n"))
    )
    with pytest.raises(ProxyUnavailable):
        await proxy.command("status")
    assert not proxy.ready and proxy.state["error"] == "control_unavailable"
    assert stdin.close.call_count == 1


async def test_startup_proxy_failure_does_not_start_app_or_create_bot(monkeypatch):
    from app.__main__ import run

    class FailedProxy:
        def __init__(self, *a):
            pass

        async def __aenter__(self):
            raise ProxyUnavailable()

        async def __aexit__(self, *a):
            pass

    entered = AsyncMock()
    monkeypatch.setattr("app.__main__.PlayerokProxy", FailedProxy)
    monkeypatch.setattr("app.__main__.run_connected", entered)
    with pytest.raises(ProxyUnavailable):
        await run(SimpleNamespace(telegram_transport="localvpn"))
    entered.assert_not_awaited()


async def test_proxy_failure_after_paid_intent_stays_unknown_without_replay(rig):
    r = rig
    t = await setup(r, "bear, rose, heart")

    original_pay = r.api.gift_sender.pay

    async def lost(prepared):
        await original_pay(prepared)
        raise ProxyUnavailable()

    r.api.gift_sender.pay = lost
    await r.worker.once()
    assert r.service.job("order")["state"] == "UNCERTAIN"
    assert r.service.steps("order")[0]["state"] == "UNCERTAIN"
    assert all(step["operation_id"] is None for step in r.service.steps("order")[1:])
    for _ in range(3):
        await r.worker.once()
    assert len(t.payments) == 1 and not r.api.refunds


async def test_localvpn_runtime_gate_blocks_new_purchase_enrollment(rig):
    r = rig
    await setup(r, "bear")
    r.service.settings = replace(r.service.settings, telegram_transport="localvpn")
    assert not r.service.runtime()
    token = active_proxy.set(SimpleNamespace(ready=False))
    try:
        assert not r.service.runtime()
    finally:
        active_proxy.reset(token)


@pytest.mark.parametrize("telethon_errors", [False, True])
async def test_aiogram_socks_remote_dns_for_api_and_file_download(monkeypatch, telethon_errors):
    if telethon_errors:
        import python_socks._errors as socks_errors

        class TelethonProxyError(ConnectionError):
            def __init__(self, message, error_code=None):
                super().__init__(message)
                self.error_code = error_code

        monkeypatch.setattr(socks_errors, "ProxyError", TelethonProxyError)
        monkeypatch.setattr(socks_errors, "ProxyConnectionError", ConnectionError)
        monkeypatch.setattr(socks_errors, "ProxyTimeoutError", ConnectionError)
    import asyncio

    seen = []

    async def socks(reader, writer):
        try:
            assert await reader.readexactly(3) == bytes([5, 1, 0])
            writer.write(bytes([5, 0]))
            await writer.drain()
            header = await reader.readexactly(5)
            assert header[:4] == bytes([5, 1, 0, 3])
            host = (await reader.readexactly(header[4])).decode()
            port = int.from_bytes(await reader.readexactly(2), "big")
            seen.append((host, port))
            writer.write(bytes([5, 5, 0, 1, 0, 0, 0, 0, 0, 0]))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(socks, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    session = TelegramNetworkSession(proxy=SOCKS_URL, localvpn=True)
    # Test transport-only port override, preserving the production remote origin.
    from aiohttp_socks import ProxyConnector

    session._connector_type = ProxyConnector
    session._connector_init.update(host="127.0.0.1", port=port, rdns=True)
    token = active_proxy.set(SimpleNamespace(ready=True))
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        with pytest.raises(TelegramNetworkError):
            await bot.get_me()
        with pytest.raises(TelegramNetworkError):
            async for _ in session.stream_content(session.api.file_url(bot.token, "photos/a.jpg")):
                pass
        assert "x" * 35 not in str(session.results)
        assert seen == [("api.telegram.org", 443), ("api.telegram.org", 443)]
    finally:
        await session.close()
        server.close()
        await server.wait_closed()
        active_proxy.reset(token)


async def test_auth_client_gets_proxy_and_preserves_existing_session_without_prompt(tmp_path, monkeypatch):
    from telethon import types

    from app.gifts.auth import main

    session = tmp_path / "existing.session"
    session.write_bytes(b"preserve-existing-session")
    settings = GiftSettings(
        1, "hash", str(session), "", "", "", False, 100, 300, telegram_transport="localvpn"
    )
    monkeypatch.setattr("app.gifts.auth.GiftSettings.load", lambda: settings)

    class Proxy:
        def __init__(self, *a):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_me.return_value = types.User(id=7)
    factory = Mock(return_value=client)
    monkeypatch.setattr("app.gifts.auth.PlayerokProxy", Proxy)
    monkeypatch.setattr("telethon.TelegramClient", factory)
    await main()
    assert factory.call_args.kwargs["proxy"] == mtproto_proxy("localvpn")
    assert factory.call_args.kwargs["request_retries"] == 0
    assert session.read_bytes() == b"preserve-existing-session"
    client.get_me.assert_awaited_once()


async def test_owner_diagnostics_exposes_only_safe_proxy_status(tmp_path):
    from app.database.store import Store
    from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession
    from tests.test_telegram_transport import production_dispatcher

    store = Store(tmp_path / "proxy-ui.sqlite")
    store.claim_first_owner(7)
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    token = active_proxy.set(
        SimpleNamespace(
            state={
                "state": "DEGRADED",
                "serverId": "new-0123456789abcdef",
                "lastCheck": "2026-10-31T00:00:00Z",
                "error": "route_unavailable",
                "telegramAvailable": False,
                "private": "never-render-this",
            }
        )
    )
    try:
        await Interaction(bot, production_dispatcher(store, CatalogAPI())).click("diagnostics")
        assert "DEGRADED" in session.calls[-1].text and "1080" in session.calls[-1].text
        assert "never-render-this" not in session.calls[-1].text
    finally:
        active_proxy.reset(token)
        await bot.session.close()
        store.close()
