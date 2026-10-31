"""Telegram routing and callback fault regressions; no production network."""

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramServerError

from app.database.store import Store
from app.telegram.keyboards import main_menu
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher


class FailingAckSession(TelegramSession):
    def __init__(self, failure):
        super().__init__()
        self.failure = failure

    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__ == "answerCallbackQuery":
            self.calls.append(method)
            raise self.failure(
                method=method,
                message=(
                    "Bad Request: query is too old and response timeout expired or query ID is invalid"
                    if self.failure is TelegramBadRequest
                    else "Request timeout error"
                ),
            )
        return await super().make_request(bot, method, timeout)


@pytest.mark.parametrize("failure", [TelegramNetworkError, TelegramBadRequest])
async def test_ack_failure_does_not_break_safe_navigation(tmp_path, failure):
    store = Store(tmp_path / "ui.sqlite")
    store.claim_first_owner(7)
    session = FailingAckSession(failure)
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        await Interaction(bot, dispatcher(store, CatalogAPI())).click("home")
        assert session.calls[-1].__api_method__ == "editMessageText"
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()
        store.close()


import asyncio
from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import Mock

from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BufferedInputFile
from aiohttp import web

from app.security.access import OwnerMiddleware
from app.telegram.callback_ack import install_callback_ack
from app.telegram.transport import TelegramNetworkSession


def production_dispatcher(store, api):
    dp = dispatcher(store, api)
    install_callback_ack(dp, OwnerMiddleware([], store))
    return dp


async def test_ack_before_fsm_lock_does_not_block_next_update(tmp_path, monkeypatch):
    monkeypatch.setattr("app.telegram.callback_ack.ACK_TIMEOUT", 0.15)

    class SlowAck(TelegramSession):
        started = asyncio.Event()

        async def make_request(self, bot, method, timeout=None):
            if method.__api_method__ == "answerCallbackQuery" and method.callback_query_id == "1":
                self.started.set()
                await asyncio.sleep(10)
            return await super().make_request(bot, method, timeout)

    store = Store(tmp_path / "slow.sqlite")
    store.claim_first_owner(7)
    session = SlowAck()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, production_dispatcher(store, CatalogAPI()))
    try:
        first = asyncio.create_task(ui.click("home"))
        await session.started.wait()
        await asyncio.wait_for(ui.click("diagnostics"), timeout=0.1)
        assert not first.done()
        await first
        assert sum(m.__api_method__ == "editMessageText" for m in session.calls) == 2
    finally:
        await bot.session.close()
        store.close()


@pytest.mark.parametrize("failure", [TelegramNetworkError, TelegramBadRequest])
async def test_unacknowledged_mutating_callback_is_not_executed(tmp_path, failure):
    store = Store(tmp_path / "gated.sqlite")
    store.claim_first_owner(7)
    store.backup = Mock()
    session = FailingAckSession(failure)
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        await Interaction(bot, production_dispatcher(store, CatalogAPI())).click("backup")
        store.backup.assert_not_called()
        assert "Действие не выполнено" in session.calls[-1].text
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()
        store.close()


@pytest.mark.parametrize("method", ["sendMessage", "editMessageText"])
async def test_ui_response_timeout_is_not_resent_and_navigation_recovers(tmp_path, method):
    class FailedResponse(TelegramSession):
        failed = False

        async def make_request(self, bot, request, timeout=None):
            if request.__api_method__ == method and not self.failed:
                self.failed = True
                self.calls.append(request)
                raise TelegramNetworkError(method=request, message="private-content-not-for-log")
            return await super().make_request(bot, request, timeout)

    store = Store(tmp_path / "response.sqlite")
    store.claim_first_owner(7)
    session = FailedResponse()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, production_dispatcher(store, CatalogAPI()))
    try:
        if method == "sendMessage":
            await ui.message("/start")
        else:
            await ui.click("home")
        assert sum(m.__api_method__ == method for m in session.calls) == 1
        await ui.click("diagnostics")
        assert "Диагностика" in session.calls[-1].text
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()
        store.close()


@pytest.fixture
async def http_worker():
    calls = []
    control = {"fail": False, "redirect": False, "delay": 0}

    async def handler(request):
        calls.append((request.method, request.path))
        if control["redirect"]:
            raise web.HTTPTemporaryRedirect("https://api.telegram.org/botsecret/getMe")
        if control["fail"]:
            return web.json_response({"ok": False, "description": "gateway down"}, status=503)
        if request.method == "GET":
            return web.Response(body=b"image-bytes")
        await request.post()
        name = request.match_info["method"]
        if name == "getMe":
            result = {"id": 123, "is_bot": True, "first_name": "Test"}
        elif name == "getUpdates":
            await asyncio.sleep(control["delay"])
            result = []
        elif name == "getFile":
            result = {"file_id": "f", "file_unique_id": "u", "file_path": "photos/a.jpg"}
        elif name in {"answerCallbackQuery", "deleteMessage"}:
            result = True
        else:
            result = {
                "message_id": 1,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": 7, "type": "private"},
                "text": "test",
            }
        return web.json_response({"ok": True, "result": result})

    app = web.Application()
    app.router.add_route("*", "/bot{token}/{method}", handler)
    app.router.add_get("/file/bot{token}/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    session = TelegramNetworkSession(
        api=TelegramAPIServer.from_base(f"http://127.0.0.1:{port}"), timeout=1, diagnostics=True
    )
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        yield bot, session, calls, control
    finally:
        await session.close()
        await runner.cleanup()


async def test_all_api_methods_and_upload_download_share_pinned_route(http_worker):
    bot, session, calls, _control = http_worker
    await bot.get_me()
    await bot.get_updates(timeout=0)
    await bot.answer_callback_query("valid-test")
    await bot.send_message(7, "private text")
    await bot.edit_message_text("edit", chat_id=7, message_id=1)
    await bot.send_photo(7, BufferedInputFile(b"fake-test-png", filename="test.png"))
    destination = BytesIO()
    await bot.download("f", destination=destination)
    await bot.delete_message(7, 1)
    assert destination.getvalue() == b"image-bytes"
    expected = {
        "getMe",
        "getUpdates",
        "answerCallbackQuery",
        "sendMessage",
        "editMessageText",
        "sendPhoto",
        "getFile",
        "downloadFile",
        "deleteMessage",
    }
    assert set(session.results) == expected
    assert all(
        r["outcome"] == "OK" and r["hostname"] == "127.0.0.1" and r["http"] == 200
        for r in session.results.values()
    )
    assert all(path.startswith(("/bot123:", "/file/bot123:")) for _, path in calls)
    evidence = str(session.results)
    assert bot.token not in evidence and "private text" not in evidence and "valid-test" not in evidence


async def test_worker_failure_recovery_long_poll_and_no_direct_fallback(http_worker):
    bot, session, calls, control = http_worker
    control["fail"] = True
    with pytest.raises(TelegramServerError):
        await bot.get_updates(timeout=0)
    assert session.results["getUpdates"]["http"] == 503
    control.update(fail=False, delay=0.12)
    assert await bot.get_updates(timeout=1) == []
    assert session.results["getUpdates"]["outcome"] == "OK"
    assert session.results["getUpdates"]["seconds"] >= 0.1
    assert len(calls) == 2
    # Changing api templates cannot silently change the pinned destination.
    session.api = TelegramAPIServer.from_base("https://api.telegram.org")
    with pytest.raises(TelegramNetworkError):
        await bot.get_me()
    assert session.results["getMe"]["blocked"] == "ROUTE_MISMATCH"
    assert len(calls) == 2


async def test_worker_redirect_cannot_forward_bot_token(http_worker):
    bot, session, calls, control = http_worker
    control["redirect"] = True
    with pytest.raises(TelegramNetworkError):
        await bot.get_me()
    assert session.results["getMe"]["blocked"] == "REDIRECT_BLOCKED"
    assert session.results["getMe"]["http"] == 307 and len(calls) == 1


async def test_profile_status_survives_optional_photo_timeout(tmp_path, monkeypatch):
    from tests.test_playerok_connection import API

    class FailedPhoto(TelegramSession):
        async def make_request(self, bot, method, timeout=None):
            if method.__api_method__ == "sendPhoto":
                self.calls.append(method)
                raise TelegramNetworkError(method=method, message="Request timeout error")
            return await super().make_request(bot, method, timeout)

    monkeypatch.setattr("app.telegram.profile_panel.profile_card", lambda *a: b"png")
    store = Store(tmp_path / "profile-network.sqlite")
    store.claim_first_owner(7)
    session = FailedPhoto()
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        await Interaction(bot, production_dispatcher(store, API())).click("profile:details")
        assert session.calls[-1].__api_method__ == "sendMessage"
        assert "Подключён" in session.calls[-1].text
        assert sum(m.__api_method__ == "sendPhoto" for m in session.calls) == 1
    finally:
        await bot.session.close()
        store.close()


async def test_all_main_menu_buttons_and_restart_via_production_dispatcher(tmp_path, monkeypatch):
    from tests.test_playerok_connection import API

    class MenuAPI(API):
        async def call(self, method, **kwargs):
            if method == "get_my_items":
                return {"items": [], "page_info": {}}
            if method == "get_chats":
                return {"chats": [], "page_info": {}}
            if method == "get_games":
                return {"games": [], "page_info": {}}
            return await super().call(method, **kwargs)

    monkeypatch.setattr("app.telegram.profile_panel.profile_card", lambda *a: b"png")
    monkeypatch.setattr(
        "app.telegram.statistics_panel.statistics_card", lambda *a, **k: b"png", raising=False
    )
    store = Store(tmp_path / "menu.sqlite")
    store.claim_first_owner(7)
    api = MenuAPI()
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        actions = [b.callback_data for row in main_menu().inline_keyboard for b in row]
        for action in actions:
            # Each screen re-enters through persisted SQLite FSM after restart.
            ui = Interaction(bot, production_dispatcher(store, api))
            start = len(session.calls)
            await ui.click(action)
            assert len(session.calls) > start + 1, action
            assert session.calls[-1].__api_method__ in {"sendMessage", "editMessageText", "sendPhoto"}, action
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()
        store.close()
