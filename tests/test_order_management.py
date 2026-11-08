import asyncio
import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.database.store import Conflict, Store, now
from app.playerok.adapter import APIError, Playerok, SafeAccount
from app.services.catalog import order_page
from app.services.order_management import OrderManagement, order_snapshot
from app.services.orders import Orders
from app.services.products import ValidationError
from app.telegram.orders_panel import OrdersPanel
from app.workers.notifications import Notifications
from app.workers.outbox import Executor, OutboxWorker


class Account:
    id = "seller"
    writes_enabled = True

    def __init__(self):
        self.calls, self.error = [], None

    @contextmanager
    def permit(self, operation):
        self.calls.append(operation)
        yield

    def send_message(self, **kwargs):
        assert kwargs["chat_id"] == "chat" and kwargs["text"] == "private code"
        if self.error:
            raise self.error
        return {"id": "message"}


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "orders.sqlite")
    store.claim_first_owner(1)
    order = {
        "id": "order",
        "status": "PAID",
        "direction": "OUT",
        "has_problem": False,
        "is_automated": False,
        "created_at": now(),
        "item": {"id": "item", "name": "Test <product>", "price": 100, "user": {"id": "seller"}},
        "user": {"id": "buyer", "username": "buyer_%"},
        "chat": {"id": "chat"},
        "logs": [],
    }
    api = SimpleNamespace(account=Account(), lock=asyncio.Lock())

    async def call(method, **kwargs):
        if method in {"get_deal", "get_delivery_deal"}:
            assert kwargs == {"deal_id": "order"}
            return deepcopy(order)
        if method == "get_item":
            return {**deepcopy(order["item"]), "status": "APPROVED"}
        if method == "get_chat":
            return {"id": "chat", "is_texting_allowed": True}
        raise AssertionError(method)

    api.call = AsyncMock(side_effect=call)
    yield store, api, order, OrderManagement(store, api)
    store.close()


async def test_manual_stock_delivery_revalidates_and_sends_only_once(setup):
    store, api, order, service = setup
    await service.fresh(1, "order")
    assert await service.add_stock(1, "order", order_snapshot(order), "private code\nprivate code") == 1
    assert not store.rows("SELECT * FROM outbox")
    first = await service.queue_delivery(1, "order", order_snapshot(order))
    assert await service.queue_delivery(1, "order", order_snapshot(order)) == first
    worker = OutboxWorker(store, Executor(store, api, None))
    await worker.once()
    await worker.once()
    assert api.account.calls == ["createChatMessage"]  # no updateDeal / auto-confirm
    assert store.rows("SELECT status FROM inventory") == [{"status": "DELIVERED"}]
    assert store.rows("SELECT status FROM orders") == [{"status": "PAID"}]
    assert store.rows("SELECT source FROM order_history WHERE source='DELIVERY_RESULT'")
    assert "private code" not in str(store.rows("SELECT * FROM order_history"))
    assert "private code" not in str(store.rows("SELECT * FROM audit_log"))


@pytest.mark.parametrize(
    "change",
    [
        {"status": "PENDING"},
        {"status": "SENT"},
        {"status": "ROLLED_BACK"},
        {"status": "FAILED"},
        {"has_problem": True},
        {"has_problem": None},
        {"is_automated": True},
        {"is_automated": None},
        {"direction": "IN"},
        {"chat": None},
    ],
)
async def test_unpaid_disputed_automated_or_incomplete_sale_cannot_queue(setup, change):
    store, _, order, service = setup
    order.update(change)
    with pytest.raises((ValidationError, Conflict)):
        await service.queue_delivery(1, "order", order_snapshot(order))
    assert not store.rows("SELECT * FROM outbox")


async def test_owner_seller_and_preview_guards(setup):
    store, api, order, service = setup
    with pytest.raises(Conflict):
        await service.fresh(2, "order")
    api.call.assert_not_called()
    with pytest.raises(Conflict, match="изменилась"):
        await service.queue_delivery(1, "order", "stale")
    order["item"]["user"]["id"] = "foreign"
    with pytest.raises(ValidationError):
        await service.prepare_delivery(1, "order")
    assert not store.rows("SELECT * FROM outbox")


async def test_readonly_or_closed_chat_cannot_queue(setup):
    store, api, order, service = setup
    api.account.writes_enabled = False
    with pytest.raises(ValidationError, match="отключены"):
        await service.queue_delivery(1, "order", order_snapshot(order))
    api.call.assert_not_called()
    api.account.writes_enabled = True
    api.call.side_effect = lambda method, **kwargs: (
        deepcopy(order) if method == "get_deal" else {"id": "chat", "is_texting_allowed": False}
    )
    with pytest.raises(ValidationError, match="Возможность"):
        await service.queue_delivery(1, "order", order_snapshot(order))
    assert not store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("race", ["rollback", "timeout"])
async def test_change_or_timeout_after_reservation_quarantines_stock(setup, race):
    store, api, order, service = setup
    await service.fresh(1, "order")
    service.orders.add_stock("item", ["private code"])
    await service.queue_delivery(1, "order", order_snapshot(order))
    if race == "rollback":
        order["status"] = "ROLLED_BACK"
    else:
        api.account.error = APIError("NETWORK_ERROR", uncertain=True)
    worker = OutboxWorker(store, Executor(store, api, None))
    await worker.once()
    assert store.rows("SELECT status FROM inventory")[0]["status"] == (
        "RESERVED" if race == "rollback" else "QUARANTINED"
    )
    assert not await worker.once()
    assert len(api.account.calls) == (0 if race == "rollback" else 1)


async def test_empty_stock_alert_is_deduplicated_and_not_hidden_by_initial_sync(setup):
    store, _, _order, service = setup
    await service.fresh(1, "order")
    assert service.orders.reserve("order") is None
    assert service.orders.reserve("order") is None
    assert len(store.rows("SELECT * FROM inbox WHERE kind='stock_empty'")) == 1
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)))
    worker = Notifications(store, bot)
    await worker.once()
    await worker.once()
    assert bot.send_message.await_count == 1
    assert "СКЛАД ПУСТ" in bot.send_message.call_args.args[1]
    assert not store.setting("delivery:item:enabled", True)


async def test_local_notes_owner_revision_filter_and_full_history(setup):
    store, _, _, service = setup
    await service.fresh(1, "order")
    with pytest.raises(Conflict):
        service.orders.annotate("order", 2, 0, True, "foreign")
    service.orders.annotate("order", 1, 0, True, "<note>" * 120)
    assert order_page(store, filter_name="PROBLEM")["total"] == 1
    assert order_page(store, search="buyer_%")["total"] == 1
    assert order_page(store, search="' OR 1=1 --")["total"] == 0
    with pytest.raises(Conflict):
        service.orders.annotate("order", 1, 0, False, "stale")
    text, markup = OrdersPanel(store, service.api).history(1, "order", 0)
    assert "Проблема (локально): да" in text
    assert any(
        button.callback_data.startswith("od:entry:") for row in markup.inline_keyboard for button in row
    )
    assert "&lt;note&gt;" in text
    assert "reportDealProblem" not in str(service.api.call.call_args_list)


async def test_card_falls_back_to_cache_without_displaying_delivery_secrets(setup):
    store, api, _, service = setup
    await service.fresh(1, "order")
    service.orders.add_stock("item", ["private code"])
    api.call.side_effect = APIError("NETWORK_ERROR")
    text, markup = await OrdersPanel(store, api).card(1, "order")
    assert "Сохранённые данные" in text and "&lt;product&gt;" in text
    assert "private code" not in text
    assert any(
        button.callback_data and button.callback_data.startswith("od:prepare:")
        for row in markup.inline_keyboard
        for button in row
    )
    # A navigation link is not dispatch authority: its prepare must still read
    # Playerok and fail closed on the unavailable connection.
    with pytest.raises(APIError, match="NETWORK_ERROR"):
        await service.prepare_delivery(1, "order")
    assert not store.rows("SELECT * FROM outbox")
    assert store.rows("SELECT status FROM inventory") == [{"status": "AVAILABLE"}]
    assert any(button.callback_data == "od:details:order" for row in markup.inline_keyboard for button in row)
    _, markup = await OrdersPanel(store, api).card(1, "order", details=True)
    assert any(
        button.url == "https://playerok.com/deal/order" for row in markup.inline_keyboard for button in row
    )


def test_state_regression_and_duplicate_poll_do_not_rewrite_history(tmp_path):
    store = Store(tmp_path / "states.sqlite")
    try:
        service = Orders(store)
        order = {"id": "order", "direction": "OUT", "status": "PAID", "item": {"id": "item"}}
        service.ingest(order)
        service.ingest(order)
        service.ingest({**order, "status": "SENT"})
        service.ingest(order)
        assert store.rows("SELECT status FROM orders")[0]["status"] == "SENT"
        assert len(store.rows("SELECT * FROM order_history")) == 2
        service.ingest({**order, "status": "FAILED"})
        service.ingest(order)
        assert store.rows("SELECT status FROM orders")[0]["status"] == "FAILED"
    finally:
        store.close()


async def test_stale_paid_event_does_not_notify_after_sent(setup):
    store, _, order, service = setup
    store.set_setting("notifications:initialized", True)
    service.orders.ingest({**order, "status": "SENT"})
    service.orders.ingest(order)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)))
    await Notifications(store, bot).once()
    assert bot.send_message.await_count == 1
    assert "ЗАКАЗ ВЫПОЛНЕН" in bot.send_message.call_args.args[1]
    assert "SENT" not in bot.send_message.call_args.args[1]
    assert "PAID" not in bot.send_message.call_args.args[1]
    assert "Оплачен" not in bot.send_message.call_args.args[1]
    assert store.rows("SELECT status FROM orders") == [{"status": "SENT"}]


async def test_problem_transition_same_status_notifies_once_and_can_reopen(setup):
    store, _, order, service = setup
    service.orders.ingest(order)
    store.set_setting("sync:health", {"status": "OK"})
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)))
    worker = Notifications(store, bot)
    await worker.once()
    service.orders.ingest({**order, "has_problem": True})
    service.orders.ingest({**order, "has_problem": True})
    await worker.once()
    await worker.once()
    assert bot.send_message.await_count == 1
    assert "ПРОБЛЕМА С ЗАКАЗОМ" in bot.send_message.call_args.args[1]
    service.orders.ingest(order)
    service.orders.ingest({**order, "has_problem": True})
    await worker.once()
    assert bot.send_message.await_count == 2


async def test_real_account_get_deal_keyword_contract_without_network_mutation():
    def respond(request):
        assert request.method == "GET"
        assert request.url.params["operationName"] == "deal"
        assert json.loads(request.url.params["variables"])["id"] == "deal"
        return httpx.Response(
            200,
            json={
                "data": {
                    "deal": {
                        "id": "deal",
                        "direction": "OUT",
                        "status": "FAILED",
                        "hasProblem": False,
                        "isAutomated": False,
                    }
                }
            },
        )

    account = SafeAccount("token=private", client=httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        result = await Playerok(account).call("get_deal", deal_id="deal")
        assert result["id"] == "deal" and result["status"] == "FAILED"
    finally:
        account.close()
