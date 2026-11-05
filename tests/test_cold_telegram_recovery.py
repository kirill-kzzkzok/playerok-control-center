"""Native control and Telegram pre-dispatch failures must not strand independent sales."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Chat, Message

from app.localvpn import NetworkSettings, PlayerokProxy, active_proxy
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.notifications import Notifications
from tests.test_stage_a_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


def native_control(tmp_path, monkeypatch):
    exe = tmp_path / "localvpn-playerok.exe"
    exe.write_bytes(b"isolated-native-control-boundary")
    native_ready = asyncio.Event()
    commands = []

    class Input:
        def write(self, data):
            commands.append(json.loads(data)["command"])

        async def drain(self):
            pass

        def close(self):
            process.returncode = 0

    class Output:
        async def readline(self):
            state = "READY" if native_ready.is_set() else "DEGRADED"
            return (
                json.dumps(
                    {
                        "state": state,
                        "address": "socks5://127.0.0.1:1080",
                        "telegramAvailable": native_ready.is_set(),
                        "generation": 1 if native_ready.is_set() else 0,
                    }
                ).encode()
                + b"\n"
            )

    async def waited():
        return 0

    process = SimpleNamespace(returncode=None, stdin=Input(), stdout=Output(), wait=waited)

    async def launch(*args, **kwargs):
        return process

    monkeypatch.setattr("app.localvpn.asyncio.create_subprocess_exec", launch)
    return NetworkSettings(executable=exe), native_ready, commands, process


async def test_cold_native_proxy_recovers_and_stock_keeps_delivering_without_telegram(
    shop, tmp_path, monkeypatch
):
    r = await shop()
    await setup_ui(r, "unique_stock")
    settings, native_ready, commands, process = native_control(tmp_path, monkeypatch)
    proxy = PlayerokProxy(settings, recover_on_start=True)
    ticks = asyncio.Queue()
    actual_sleep = asyncio.sleep

    async def controlled_timer(seconds, *args, **kwargs):
        if asyncio.current_task() is proxy.monitor:
            await ticks.get()
        else:
            await actual_sleep(seconds, *args, **kwargs)

    monkeypatch.setattr("app.localvpn.asyncio.sleep", controlled_timer)
    async with proxy:
        assert active_proxy.get() is proxy and not proxy.ready
        Orders(r.store).ingest(r.api.order("stock-during-telegram-outage"))
        await EnrollmentWorker(r.store, r.api).once()
        for _ in range(8):
            ChainWorker(r.store, r.api).once()
            await r.worker.once()
        assert len(r.store.rows("SELECT 1 FROM inventory WHERE status='DELIVERED'")) == 1
        assert not r.api.remote.payments
        native_ready.set()
        ticks.put_nowait(None)
        await proxy.confirm()
        assert proxy.ready
    assert process.returncode == 0 and commands[0] == "start"
    assert active_proxy.get() is None


async def test_known_proxy_preflight_failure_keeps_owner_notice_pending_until_route_returns(
    tmp_path, monkeypatch
):
    from aiogram.client.session.aiohttp import AiohttpSession

    from app.database.store import Store
    from app.telegram.transport import TelegramNetworkSession

    store = Store(tmp_path / "notice.sqlite3")
    store.claim_first_owner(7)
    store.set_setting("notifications:initialized", True)
    Orders(store).add_stock("lot", ["one"])
    with store.transaction() as db:
        Orders.stock_level(db, "lot")
    calls = []

    async def http_boundary(self, bot, method, timeout=None):
        from aiohttp import ClientConnectionError
        from yarl import URL

        try:
            await self._request_start(None, None, SimpleNamespace(url=URL("https://api.telegram.org")))
        except ClientConnectionError:
            raise TelegramNetworkError(method=method, message="Playerok SOCKS service is not ready") from None
        calls.append(method.__api_method__)
        return Message(message_id=42, date=0, chat=Chat(id=7, type="private"), text="owner notice")

    monkeypatch.setattr(AiohttpSession, "make_request", http_boundary)
    session = TelegramNetworkSession(proxy="socks5://127.0.0.1:1080", localvpn=True)
    bot = Bot("123:" + "x" * 35, session=session)
    route = SimpleNamespace(ready=False)
    token = active_proxy.set(route)
    try:
        await Notifications(store, bot).once()
        assert not calls
        assert store.rows("SELECT status FROM notifications") == [{"status": "PENDING"}]
        route.ready = True
        store.execute("UPDATE notifications SET send_retry_at=0")
        await Notifications(store, bot).once()
        await Notifications(store, bot).once()
        assert calls == ["sendMessage"]
        assert store.rows("SELECT status FROM notifications") == [{"status": "SUCCEEDED"}]
    finally:
        active_proxy.reset(token)
        await session.close()
        store.close()


@pytest.mark.parametrize("poll_failure", [None, "network", "returned"])
async def test_real_bootstrap_processes_new_paid_stock_before_cold_telegram_connection_recovers(
    shop, tmp_path, monkeypatch, poll_failure
):
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.types import User, WebhookInfo

    from app import __main__ as application
    from app.security.instance import InstanceLock

    r = await shop()
    original_polling = application.Dispatcher.start_polling
    attempts = 0

    async def polling(dispatcher, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1 and poll_failure:
            if poll_failure == "network":
                raise OSError("injected transport failure")
            return
        return await original_polling(dispatcher, *args, **kwargs)

    monkeypatch.setattr(application.Dispatcher, "start_polling", polling)
    await setup_ui(r, "unique_stock")
    settings, native_ready, _commands, process = native_control(tmp_path, monkeypatch)
    monkeypatch.setattr(application.NetworkSettings, "load", lambda *args, **kwargs: settings)
    monkeypatch.chdir(tmp_path)
    path = r.store.path
    r.store.close()
    # Marketplace SDK transport boundary; all application services/workers remain real.
    r.api.order("startup-paid-stock")
    old_call = r.api.call

    async def market(method, **kwargs):
        if method == "get":
            return {"id": "seller", "username": "seller", "is_blocked": False, "can_publish_items": True}
        if method == "get_transactions":
            return r.api.page("transactions", [])
        if method == "get_transaction_providers":
            return []
        if method == "get_user":
            return {"id": kwargs.get("id", "buyer"), "username": "buyer", "role": "USER"}
        return await old_call(method, **kwargs)

    r.api.call = market
    r.api.account.close = lambda: None
    monkeypatch.setattr(application, "SafeAccount", lambda *args, **kwargs: r.api.account)
    monkeypatch.setattr(application, "Playerok", lambda account: r.api)
    stock_delivered = asyncio.Event()
    polling_started = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_send = r.api.send_message

    def send_message(chat_id, text):
        result = original_send(chat_id, text)
        if text.startswith("isolated-unit-"):
            loop.call_soon_threadsafe(stock_delivered.set)
        return result

    r.api.account.send_message = send_message
    http_calls = []

    async def telegram_http(self, bot, method, timeout=None):
        from yarl import URL

        await self._request_start(None, None, SimpleNamespace(url=URL("https://api.telegram.org")))
        http_calls.append(method.__api_method__)
        if method.__api_method__ == "getMe":
            if http_calls.count("getMe") > 1:
                raise TelegramNetworkError(method=method, message="connection lost after identity check")
            return User(id=123, is_bot=True, first_name="Shop")
        if method.__api_method__ == "getWebhookInfo":
            return WebhookInfo(url="", has_custom_certificate=False, pending_update_count=0)
        if method.__api_method__ == "getUpdates":
            polling_started.set()
            await asyncio.Event().wait()
        return Message(
            message_id=80 + len(http_calls),
            date=0,
            chat=Chat(id=7, type="private"),
            text="owner notification",
        )

    monkeypatch.setattr(AiohttpSession, "make_request", telegram_http)
    config = SimpleNamespace(
        database_path=path,
        telegram_token="123:" + "x" * 35,
        playerok_header="token=isolated",
        telegram_transport="localvpn",
        use_proxy=False,
        owner_ids=[7],
        first_start_claim=False,
        writes_enabled=True,
        automation_enabled=True,
        realtime_enabled=False,
        auto_accept_agreements=False,
    )
    with InstanceLock(str(path) + ".lock") as instance_lock:
        task = asyncio.create_task(application.run(config, instance_lock=instance_lock))
        delivered = asyncio.create_task(stock_delivered.wait())
        try:
            done, _ = await asyncio.wait({task, delivered}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
            assert delivered in done, "Playerok stock worker never started during Telegram outage"
            assert not http_calls and not r.api.remote.payments
            native_ready.set()
            polling = asyncio.create_task(polling_started.wait())
            try:
                done, _ = await asyncio.wait({task, polling}, timeout=12, return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    await task
                assert polling in done
                assert http_calls.count("getMe") == 1 and "getWebhookInfo" in http_calls
            finally:
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
        finally:
            delivered.cancel()
            task.cancel()
            await asyncio.gather(task, delivered, return_exceptions=True)
    assert process.returncode == 0
    from app.database.store import Store

    reopened = Store(path)
    try:
        assert len(reopened.rows("SELECT 1 FROM inventory WHERE status='DELIVERED'")) == 1
        assert reopened.rows("SELECT status FROM outbox WHERE kind='delivery'") == [{"status": "SUCCEEDED"}]
    finally:
        reopened.close()
