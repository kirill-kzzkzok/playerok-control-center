"""Cold system-tunnel recovery must not strand independent paid sales."""

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Chat, Message

from tests.test_stage_a_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


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
    native_ready = asyncio.Event()

    def check():
        if not native_ready.is_set():
            raise OSError("System tunnel unavailable")
        return 1

    monkeypatch.setattr("app.telegram.transport.HappRoute", lambda: SimpleNamespace(check=check))
    monkeypatch.setattr("app.telegram.system_tunnel.happ_required", lambda mode: mode == "direct")
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
        telegram_transport="direct",
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
    from app.database.store import Store

    reopened = Store(path)
    try:
        assert len(reopened.rows("SELECT 1 FROM inventory WHERE status='DELIVERED'")) == 1
        assert reopened.rows("SELECT status FROM outbox WHERE kind='delivery'") == [{"status": "SUCCEEDED"}]
    finally:
        reopened.close()
