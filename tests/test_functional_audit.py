"""Audit regressions: real Telegram dispatcher and real guarded HTTP mutation transport."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict

import httpx
import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store
from app.playerok.adapter import SafeAccount
from app.playerok.thread_calls import close_calls
from app.services.automation import Automation, DeliveryPolicy
from app.services.fulfillment import Fulfillment
from app.services.orders import Orders
from app.telegram.storage import SQLiteStorage
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.outbox import Executor, OutboxWorker
from app.workers.sync import Synchronizer
from tests.test_operations import FakeAPI
from tests.test_stock_auto_cycle import Marketplace
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


@pytest.fixture
async def rig(tmp_path):
    store, api, telegram = Store(tmp_path / "audit.sqlite"), FakeAPI(), TelegramSession()
    store.claim_first_owner(7)
    requests = []
    order = {
        "id": "order",
        "direction": "OUT",
        "status": "PAID",
        "has_problem": False,
        "is_automated": False,
        "item": {"id": "item", "user": {"id": "seller", "role": "USER"}},
        "chat": {"id": "chat"},
    }

    def respond(request):
        body = json.loads(request.content)
        requests.append(body["operationName"])
        if body["operationName"] == "updateDeal":
            return httpx.Response(200, json={"data": {"updateDeal": {"id": "order", "status": "SENT"}}})
        return httpx.Response(
            200,
            json={"data": {"createChatMessage": {"id": "receipt", "text": "unit", "user": {"id": "seller"}}}},
        )

    api.account = SafeAccount(
        "token=mock-only", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    api.account.id = "seller"
    original = api.call
    checked = asyncio.Event()

    async def read(method, **kwargs):
        if method == "get_deal":
            return deepcopy(order)
        if method == "get_chat":
            checked.set()
            return {"id": "chat", "is_texting_allowed": True}
        return await original(method, **kwargs)

    api.call = read
    bot = Bot("123:" + "x" * 35, session=telegram)
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    yield store, api, ui, state, checked, order, requests, telegram
    await close_calls(api)
    api.account.close()
    await bot.session.close()
    store.close()


@pytest.mark.parametrize("change", ["refund", "local_problem", "server_problem"])
async def test_manual_delivery_late_order_change_prevents_http(rig, change):
    store, api, ui, state, checked, order, requests, _telegram = rig
    Orders(store).ingest(order)
    Orders(store).add_stock("item", ["unit"])
    await ui.click("od:prepare:order")
    await ui.click("od:apply:" + (await state.get_data())["order_token"])
    checked.clear()
    await api.lock.acquire()
    task = asyncio.create_task(OutboxWorker(store, Executor(store, api, None)).once())
    try:
        await asyncio.wait_for(checked.wait(), 2)
        if change == "refund":
            Orders(store).ingest({**order, "status": "ROLLED_BACK"})
        elif change == "server_problem":
            Orders(store).ingest({**order, "has_problem": True})
        else:
            Orders(store).annotate("order", 7, 0, True, "Owner stopped delivery")
    finally:
        api.lock.release()
    await task
    assert requests == []
    assert store.rows("SELECT status FROM inventory") == [{"status": "QUARANTINED"}]


@pytest.mark.parametrize(
    "section",
    [
        "item:item",
        "result:list:0",
        "delivered:0",
        "diagnostics",
        "sp:connections",
        "stock:list:item:AVAILABLE:0",
    ],
)
async def test_leaving_input_flow_clears_sensitive_fsm(rig, section):
    _store, _api, ui, state, _checked, _order, _requests, _telegram = rig
    await ui.click("stock:add:item")
    assert await state.get_state() == "stock:input"
    await ui.click(section)
    assert await state.get_state() is None


async def test_fulfillment_late_refund_prevents_http(rig):
    store, api, _ui, _state, _checked, order, requests, _telegram = rig
    service = Fulfillment(store, api)
    prepared = await service.prepare(7, "order")
    await service.queue(7, "order", prepared["snapshot"], {}, True, "attested")
    validated = asyncio.Event()
    # Observe the executor's validation, before it waits for mutation serialization.
    original_read = api.call

    async def read(method, **kwargs):
        value = await original_read(method, **kwargs)
        if method == "get_deal":
            validated.set()
        return value

    api.call = read
    await api.lock.acquire()
    task = asyncio.create_task(OutboxWorker(store, Executor(store, api, None)).once())
    try:
        await asyncio.wait_for(validated.wait(), 2)
        Orders(store).ingest({**order, "status": "ROLLED_BACK"})
    finally:
        api.lock.release()
    await task
    assert requests == []


async def test_owner_confirmed_uncertain_last_delivery_can_finish_without_resend(tmp_path):
    store, api, telegram = Store(tmp_path / "uncertain.sqlite"), Marketplace("lost_ack"), TelegramSession()
    store.claim_first_owner(7)
    bot = Bot("123:" + "x" * 35, session=telegram)
    original = api.call

    async def call(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_item":
            result.update(status="APPROVED", name="Real stock fixture")
        return result

    api.call = call
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        sync = Synchronizer(store, api, Orders(store))
        await sync.once()
        await ui.click("stock:add:item")
        await ui.message("unit")
        await ui.click("stock:commit:" + (await state.get_data())["stock_pending"]["nonce"])
        await Automation(store, api).save(
            7,
            "item",
            0,
            asdict(DeliveryPolicy(enabled=True, mode="stock", auto_enroll=True, mark_sent=True)),
            future_confirmed=True,
        )
        api.paid_order()
        await sync.once()
        await EnrollmentWorker(store, api).once()
        ChainWorker(store, api).once()
        await OutboxWorker(store, Executor(store, api, None)).once()
        ChainWorker(store, api).once()
        ident = store.rows("SELECT id FROM outbox")[0]["id"]
        # Fresh dispatcher/storage instance, retaining all evidence in SQLite.
        ui.dp = dispatcher(store, api)
        await ui.click("ops:confirm:" + ident)
        await ui.message("receipt")
        await ui.message("Проверены отправитель и товар по истории сообщения")
        await ui.click("ops:apply:" + (await state.get_data())["resolution"]["nonce"])
        assert store.rows("SELECT status FROM inventory") == [{"status": "DELIVERED"}]
        assert store.rows("SELECT status FROM delivery_chains") == [{"status": "NEEDS_FULFILLMENT"}]
        await ui.click("ff:start:order")
        for action in ("check", "continue", "submit"):
            await ui.click("ff:" + (await state.get_data())["fulfillment"]["nonce"] + ":" + action)
        await OutboxWorker(store, Executor(store, api, None)).once()
        assert api.orders["order"]["status"] == "SENT"
        assert api.mutations == ["send", "fulfill"]
    finally:
        await bot.session.close()
        store.close()
