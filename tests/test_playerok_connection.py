import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot

from app.config import Config
from app.database.store import Store
from app.playerok.adapter import APIError
from app.services.playerok_connection import PlayerokConnection
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


class API:
    def __init__(self):
        self.account = SimpleNamespace(
            id="seller", writes_enabled=True, automation_enabled=True, close=Mock()
        )
        self.calls = []
        self.error = None
        self.wait = None
        self.started = asyncio.Event()

    async def call(self, method, **kwargs):
        self.calls.append(method)
        assert method == "get"
        self.started.set()
        if self.wait:
            await self.wait.wait()
        if self.error:
            raise self.error
        return {"id": self.account.id, "username": "shopname", "profile": {}, "is_blocked": False}


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "connection.sqlite")
    store.claim_first_owner(7)
    yield store, API()
    store.close()


async def test_fresh_check_confirmed_binding_and_cached_status_not_authentication(rig):
    store, api = rig
    store.set_setting(
        "playerok:verification",
        {"status": "CONNECTED", "last_success": "2026-10-30T00:00:00+00:00", "username": "old"},
    )
    store.set_setting("playerok:connection", "OK")
    connection = PlayerokConnection(store, api)
    assert not api.account.writes_enabled and connection.state["status"] == "UNKNOWN"
    result = await connection.check()
    assert result["status"] == "CONNECTED" and result["last_success"] and result["last_attempt"]
    assert store.setting("playerok:bound_account_id") == "seller" and api.calls == ["get"]
    assert api.account.writes_enabled and api.account.automation_enabled


@pytest.mark.parametrize(
    "error,status",
    [
        (APIError("SESSION_EXPIRED"), "DISCONNECTED"),
        (APIError("UNAUTHORIZED_OR_CHALLENGE", diagnostics={"http_status": 401}), "DISCONNECTED"),
        (APIError("UNAUTHORIZED_OR_CHALLENGE", diagnostics={"http_status": 403}), "UNKNOWN"),
        (APIError("NETWORK_ERROR"), "UNKNOWN"),
        (APIError("HTTP_503"), "UNKNOWN"),
        (TimeoutError("private-cookie"), "UNKNOWN"),
    ],
)
async def test_expired_auth_and_network_are_distinguished_and_writes_closed(rig, error, status):
    store, api = rig
    connection = PlayerokConnection(store, api)
    await connection.check(cooldown=0)
    last = connection.state["last_success"]
    api.error = error
    result = await connection.check(cooldown=0)
    assert result["status"] == status and result["last_success"] == last
    assert not api.account.writes_enabled and not api.account.automation_enabled
    assert "private-cookie" not in str(result)
    api.error = None
    assert (await connection.check(cooldown=0))["status"] == "CONNECTED"


async def test_other_account_never_adopts_database(rig):
    store, api = rig
    store.set_setting("playerok:bound_account_id", "old-shop")
    connection = PlayerokConnection(store, api)
    result = await connection.check()
    assert result["reason"] == "ACCOUNT_MISMATCH" and result["status"] == "DISCONNECTED"
    assert store.setting("playerok:bound_account_id") == "old-shop" and not api.account.writes_enabled


async def test_concurrent_checks_and_immediate_double_click_send_only_one_read(rig):
    store, api = rig
    connection = PlayerokConnection(store, api)
    api.wait = asyncio.Event()
    task = asyncio.create_task(connection.check())
    await api.started.wait()
    assert (await connection.check())["busy"] is True
    api.wait.set()
    await task
    assert (await connection.check())["busy"] is True and api.calls == ["get"]


async def test_dispatcher_profile_check_failure_local_diagnostics_reconnect_no_mutations(rig, monkeypatch):
    store, api = rig
    monkeypatch.setattr("app.telegram.profile_panel.profile_avatar", AsyncMock(return_value=None))
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(store, api))
    try:
        api.error = APIError("SESSION_EXPIRED")
        await ui.click("profile")
        assert "🔴 Не подключён" in session.calls[-1].text
        assert any(
            b.callback_data == "profile:check"
            for r in session.calls[-1].reply_markup.inline_keyboard
            for b in r
        )
        await ui.click("diagnostics")
        await ui.click("home")
        assert not api.account.writes_enabled
        api.error = None
        api.connection.last_started = None
        await ui.click("profile:check")
        assert "🟢 Подключён" in session.calls[-1].text and "shopname" in session.calls[-1].text
        await ui.click("profile:check")
        assert "Новый запрос не отправлен" in session.calls[-1].text
        assert api.calls == ["get", "get"] and store.setting("playerok:bound_account_id") == "seller"
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()


async def test_restart_with_stored_success_still_rechecks_expired_auth(rig):
    store, api = rig
    first = PlayerokConnection(store, api)
    await first.check()
    api.account.writes_enabled = True
    api.account.automation_enabled = True
    second = PlayerokConnection(store, api)
    api.error = APIError("SESSION_EXPIRED")
    result = await second.check()
    assert result["status"] == "DISCONNECTED" and not api.account.writes_enabled
    assert api.calls == ["get", "get"]


async def test_startup_keeps_local_panel_when_playerok_unavailable(rig, monkeypatch):
    from app.__main__ import run

    store, api = rig
    api.error = APIError("SESSION_EXPIRED")
    monkeypatch.setattr(store, "backup", Mock())
    monkeypatch.setattr(store, "close", Mock())
    monkeypatch.setattr("app.__main__.Store", lambda _: store)
    monkeypatch.setattr("app.__main__.SafeAccount", lambda *a, **k: api.account)
    monkeypatch.setattr("app.__main__.Playerok", lambda _: api)
    session = SimpleNamespace(middleware=Mock(), close=AsyncMock())
    bot = SimpleNamespace(
        me=AsyncMock(), session=session, get_webhook_info=AsyncMock(return_value=SimpleNamespace(url=""))
    )
    monkeypatch.setattr("app.__main__.Bot", lambda *a, **k: bot)
    monkeypatch.setattr("app.__main__.AiohttpSession", lambda **k: session)
    monkeypatch.setattr("app.__main__.Supervisor.start", lambda *a: asyncio.create_task(asyncio.sleep(3600)))
    entered = []

    async def polling(*args, **kwargs):
        entered.append(True)
        assert api.connection.state["status"] == "DISCONNECTED"
        assert not api.account.writes_enabled and not api.account.automation_enabled
        args[0]._stop_signal = asyncio.Event()
        args[0]._stop_signal.set()

    monkeypatch.setattr("app.__main__.Dispatcher.start_polling", polling)
    config = Config(
        "123:" + "x" * 35,
        "token=mock",
        frozenset({7}),
        writes_enabled=True,
        automation_enabled=True,
        database_path=store.path,
    )
    await run(config)
    assert entered == [True] and api.calls == ["get"] and not store.rows("SELECT * FROM outbox")


async def test_native_transport_cannot_write_after_unknown_or_wrong_identity(rig):
    import json

    import httpx

    from app.playerok.adapter import Playerok, SafeAccount
    from app.playerok.thread_calls import close_calls
    from playerokapi.enums import ItemDealStatuses

    store, _ = rig
    seen = []
    failed = False

    def respond(request):
        data = json.loads(request.content) if request.method == "POST" else dict(request.url.params)
        seen.append(data["operationName"])
        if failed:
            return httpx.Response(401, json={"message": "private token must not be reflected"})
        if data["operationName"] == "viewer":
            return httpx.Response(
                200, json={"data": {"viewer": {"id": "seller", "username": "shop", "isBlocked": False}}}
            )
        if data["operationName"] == "user":
            return httpx.Response(
                200,
                json={
                    "data": {"user": {"__typename": "User", "id": "seller", "profile": {"username": "shop"}}}
                },
            )
        raise AssertionError("Unexpected mutation or read")

    account = SafeAccount(
        "token=mock-only", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    api = Playerok(account)
    connection = PlayerokConnection(store, api)
    try:
        assert (await connection.check())["status"] == "CONNECTED"
        failed = True
        with pytest.raises(APIError):
            await api.call("get_item", id="item")
        assert connection.state["status"] == "DISCONNECTED" and not account.writes_enabled
        before = len(seen)
        with pytest.raises(APIError), account.permit("updateDeal"):
            account.update_deal("order", ItemDealStatuses.ROLLED_BACK)
        with pytest.raises(APIError):
            await api.call("get_item", id="item")
        assert len(seen) == before and all(name in {"viewer", "user", "item"} for name in seen)
        assert "private token" not in str(connection.state)
    finally:
        await close_calls(api)
        account.close()


async def test_no_changes_message_on_repeated_refresh_is_safe(rig):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    from app.telegram.profile_panel import ProfilePanel

    store, api = rig
    await PlayerokConnection(store, api).check()
    message = SimpleNamespace(
        chat=SimpleNamespace(id=7),
        edit_text=AsyncMock(
            side_effect=TelegramBadRequest(
                method=EditMessageText(text="x"), message="Bad Request: message is not modified"
            )
        ),
    )
    await ProfilePanel(api, store).handle(message, refresh=True)
    assert api.calls == ["get"]


async def test_forged_profile_check_is_rejected_by_real_owner_middleware(rig):
    store, api = rig
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(store, api))
    try:
        await ui.click("profile:check", owner=8)
        assert api.calls == [] and not hasattr(api, "connection")
    finally:
        await bot.session.close()


async def test_slow_completed_probe_also_has_cooldown_for_queued_double_click(rig):
    import time

    store, api = rig
    connection = PlayerokConnection(store, api)
    api.wait = asyncio.Event()
    task = asyncio.create_task(connection.check())
    await api.started.wait()
    connection.last_started = time.monotonic() - 10  # probe took longer than the cooldown
    api.wait.set()
    await task
    assert (await connection.check())["busy"] is True
    assert api.calls == ["get"]


async def test_server_blocked_account_is_disconnected_not_a_network_failure(rig):
    store, api = rig
    api.call = AsyncMock(return_value={"id": "seller", "username": "shop", "is_blocked": True})
    result = await PlayerokConnection(store, api).check()
    assert result["status"] == "DISCONNECTED" and result["reason"] == "ACCOUNT_UNAVAILABLE"
    assert not api.account.writes_enabled and not store.setting("playerok:bound_account_id")
