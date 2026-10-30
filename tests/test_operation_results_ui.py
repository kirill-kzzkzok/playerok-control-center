from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store
from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.services.products import Products
from app.telegram.operation_results import OperationResults
from app.telegram.storage import SQLiteStorage
from app.workers.notifications import Notifications
from app.workers.outbox import Executor, OutboxWorker
from tests.test_operations import FakeAPI
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


@pytest.fixture
async def rig(tmp_path):
    store, api, session = Store(tmp_path / "results.sqlite"), FakeAPI(), TelegramSession()
    store.claim_first_owner(7)
    bot = Bot("123:" + "x" * 35, session=session)
    original = api.call

    async def call(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_item":
            result.update(price=1000, raw_price=1000)
        return result

    api.call = call
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    yield store, api, session, bot, ui, state
    await bot.session.close()
    store.close()


@pytest.mark.parametrize("kind", ["create", "publish", "discount", "edit", "delete"])
async def test_real_handlers_queue_then_notify_owner_confirmed_result_once(rig, kind):
    store, api, session, bot, ui, state = rig
    if kind == "create":
        draft = store.draft(
            7,
            {
                "step": "preview",
                "category_id": "c",
                "obtaining_type_id": "o",
                "fields": {"__comment": "private-buyer-data"},
                "photos": [],
                "name": "Product name",
                "description": "Product description",
                "price": 1000,
                "delivery": "manual",
            },
        )
        await ui.click("draft:" + draft)
        token = (await state.get_data())["view_token"]
        await ui.click(f"w:{token}:submit")
    else:
        action = {"publish": "publish", "discount": "discount", "edit": "editname", "delete": "delete"}[kind]
        await ui.click(action + ":item")
        if kind in {"discount", "edit"}:
            await ui.message("749" if kind == "discount" else "New product name")
        token = (await state.get_data())["confirm_token"]
        await ui.click("commit:" + token)
    assert "QUEUED" in session.calls[-1].text
    assert "SUCCEEDED" not in session.calls[-1].text
    ident = store.rows("SELECT id FROM outbox")[0]["id"]
    assert store.rows("SELECT owner_id FROM operation_receipts") == [{"owner_id": 7}]
    if kind == "discount":
        api.account.update_item = lambda **kwargs: {"id": kwargs["id"], "price": 749, "raw_price": 1000}
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    ui.dp = dispatcher(store, api)
    await Notifications(store, bot).once()
    assert "SUCCEEDED" in session.calls[-1].text
    if kind == "create":
        assert "Product name" in session.calls[-1].text and "remote1" in session.calls[-1].text
    if kind == "publish":
        assert "PENDING_APPROVAL" in session.calls[-1].text and "Ожидает модерации" in session.calls[-1].text
    if kind == "discount":
        assert "749" in session.calls[-1].text and "1000" in session.calls[-1].text
    count = len(session.calls)
    await Notifications(store, bot).once()
    assert len(session.calls) == count
    await ui.click("result:show:" + ident)
    assert "SUCCEEDED" in session.calls[-1].text
    for method in session.calls:
        assert "private-buyer-data" not in (getattr(method, "text", None) or "")


async def test_uncertain_operation_and_unknown_telegram_ack_never_offer_replay(rig):
    store, api, _session, _bot, ui, state = rig
    await ui.click("delete:item")
    await ui.click("commit:" + (await state.get_data())["confirm_token"])

    def lost(**kwargs):
        raise APIError("NETWORK_ERROR", uncertain=True)

    api.account.remove_item = lost
    worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
    await worker.once()
    ident = store.rows("SELECT id FROM outbox")[0]["id"]
    text, markup = OperationResults(store).show(7, ident)
    assert "UNCERTAIN" in text
    assert not any("commit:" in (b.callback_data or "") for row in markup.inline_keyboard for b in row)
    failed = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("do not persist private error")))
    await OperationResults(store).notify(failed)
    await OperationResults(store).notify(failed)
    assert failed.send_message.await_count == 1
    assert store.rows("SELECT notice_status FROM operation_receipts") == [{"notice_status": "UNCERTAIN"}]
    assert not await worker.once()


async def test_delivered_waiting_screen_requires_successful_receipt_and_stock_state(rig):
    store, _api, session, _bot, ui, _state = rig
    order = {
        "id": "order",
        "status": "PAID",
        "direction": "OUT",
        "item": {"id": "item", "name": "Stock product"},
        "chat": {"id": "chat"},
    }
    Orders(store).ingest(order)
    Orders(store).add_stock("item", ["private-unit"])
    Orders(store).reserve("order", actor=7)
    await ui.click("delivered:0")
    assert "Таких заказов нет" in session.calls[-1].text
    action = store.claim()
    store.finish(action["id"], action["lease_token"], "SUCCEEDED", result={"message_id": "receipt"})
    await ui.click("delivered:0")
    assert "серверный статус заказа ещё PAID" in session.calls[-1].text
    assert any(
        b.callback_data == "ff:start:order"
        for row in session.calls[-1].reply_markup.inline_keyboard
        for b in row
    )
    assert not store.rows("SELECT 1 FROM outbox WHERE kind='fulfill'")
    Orders(store).ingest({**order, "status": "SENT"})
    await ui.click("delivered:0")
    assert "Таких заказов нет" in session.calls[-1].text
