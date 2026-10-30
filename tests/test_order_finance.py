from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, Store
from app.services.order_finance import OrderFinance, profit_statistics, source_fingerprint
from app.services.orders import Orders
from app.services.transactions import Transactions
from app.telegram.storage import SQLiteStorage
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher

AT = datetime(2026, 10, 29, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "costs.sqlite")
    value.claim_first_owner(7)
    value.set_setting("playerok:account_id", "seller")
    yield value
    value.close()


def sale(store, ident="order", status="CONFIRMED", seller="seller"):
    Orders(store).ingest(
        {
            "id": ident,
            "direction": "OUT",
            "status": status,
            "created_at": "2026-10-28T10:00:00Z",
            "item": {"id": "product", "price": 100, "user": {"id": seller}},
        }
    )


def receipt(store, ident="receipt", order_id="order", value=90, **changes):
    Transactions(store, "seller").ingest(
        {
            "id": ident,
            "operation": "SELL",
            "direction": "IN",
            "status": "CONFIRMED",
            "currency": "RUB",
            "value": value,
            "created_at": "2026-10-28T10:00:00Z",
            "completed_at": AT.isoformat(),
            "props": {"deal_id": order_id},
            **changes,
        }
    )


def expense(store, ident="premium", operation="ITEM_PREMIUM_PRIORITY", value=20, **changes):
    receipt(store, ident, None, value, operation=operation, direction="OUT", **changes)


def costs(store, ident="order", cost=50, other=0):
    service = OrderFinance(store, "seller")
    revision = service.view(7, ident)["basis"]["revision"]
    service.set_cost(7, ident, revision, "cost", cost)
    service.set_cost(7, ident, revision + 1, "other_expenses", other)
    return service


def review(service, ident="order"):
    value = service.view(7, ident)
    service.review(7, ident, value["basis"]["revision"], value["fingerprint"])
    return service.view(7, ident)


def test_historical_cost_explicit_zero_and_owner_review_required(store):
    sale(store)
    receipt(store)
    store.set_setting("finance:7:cost:product", "1")
    service = OrderFinance(store, "seller")
    value = service.view(7, "order")
    assert value["cost"] is None and value["profit"] is None
    service.set_cost(7, "order", 0, "cost", 50)
    value = service.view(7, "order")
    assert value["other_expenses"] is None
    assert value["candidate_profit"] is None
    service.set_cost(7, "order", 1, "other_expenses", 0)
    value = service.view(7, "order")
    assert value["candidate_profit"] == 40
    assert value["profit"] is None
    assert review(service)["profit"] == 40
    service.set_cost(7, "order", 3, "cost", None)
    assert service.view(7, "order")["profit"] is None
    assert store.rows("SELECT outcome FROM audit_log WHERE action='REVIEW_ORDER_COST'") == [
        {"outcome": "OWNER_ATTESTATION"}
    ]


def test_premium_allocation_one_time_and_withdrawal_only_retained_difference(store):
    sale(store)
    receipt(store)
    expense(store)
    expense(store, "withdraw", "WITHDRAW", 100, props={"amount_after_conversion_rub": 94})
    service = costs(store)
    service.allocate(7, "order", 2, "premium", 20)
    service.allocate(7, "order", 3, "withdraw", 6)
    value = review(service)
    assert value["allocated"] == 26
    assert value["profit"] == 14
    with pytest.raises(Conflict, match="превышает"):
        service.allocate(7, "order", 5, "withdraw", 100)
    assert service.view(7, "order")["profit"] == 14
    assert not store.rows("SELECT * FROM financial_operations")
    assert not store.rows("SELECT * FROM promotion_expenses")


def test_allocation_updates_replace_not_append_and_cannot_overbook_other_orders(store):
    sale(store)
    sale(store, "other")
    expense(store)
    service = costs(store)
    service.allocate(7, "order", 2, "premium", 15)
    with pytest.raises(Conflict):
        service.allocate(7, "other", 0, "premium", 6)
    service.allocate(7, "order", 3, "premium", 10)
    service.allocate(7, "other", 0, "premium", 10)
    assert len(store.rows("SELECT * FROM order_expense_allocations")) == 2
    assert (
        sum(float(row["amount"]) for row in store.rows("SELECT amount FROM order_expense_allocations")) == 20
    )


def test_concurrent_allocations_across_database_connections_enforce_source_capacity(store):
    sale(store, "a")
    sale(store, "b")
    expense(store)

    def allocate(ident):
        connection = Store(store.path)
        try:
            OrderFinance(connection, "seller").allocate(7, ident, 0, "premium", 15)
            return True
        except Conflict:
            return False
        finally:
            connection.close()

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(allocate, ["a", "b"])) == 1
    assert len(store.rows("SELECT * FROM order_expense_allocations")) == 1


def test_repeat_poll_does_not_invalidate_review_but_economic_change_does(store):
    sale(store)
    receipt(store)
    expense(store)
    service = costs(store)
    service.allocate(7, "order", 2, "premium", 10)
    assert review(service)["profit"] == 30
    receipt(store)
    expense(store)
    assert service.view(7, "order")["profit"] == 30
    receipt(store, value=95)
    value = service.view(7, "order")
    assert value["profit"] is None and value["candidate_profit"] == 35
    assert review(service)["profit"] == 35
    expense(store, value=5)
    value = service.view(7, "order")
    assert value["profit"] is None and value["errors"]
    # Removal must remain possible after a source becomes invalid or rolls back.
    expense(store, status="ROLLED_BACK")
    service.allocate(7, "order", value["basis"]["revision"], "premium", 0)
    assert not service.view(7, "order")["allocations"]
    assert review(service)["profit"] == 45


def test_receipt_ambiguity_missing_currency_date_refund_and_cost_loss_remain_unknown(store):
    sale(store)
    receipt(store)
    service = costs(store, cost=200)
    assert review(service)["profit"] == -110
    receipt(store, "second")
    assert service.view(7, "order")["profit"] is None
    receipt(store, "second", status="ROLLED_BACK")
    assert service.view(7, "order")["profit"] == -110
    receipt(store, currency="USD")
    assert service.view(7, "order")["candidate_profit"] is None
    receipt(store)
    sale(store, status="ROLLED_BACK")
    assert service.view(7, "order")["profit"] is None


def test_invalid_sources_and_actor_revision_source_change_rejected_atomically(store):
    sale(store)
    receipt(store)
    expense(store)
    expense(store, "buy", "BUY")
    expense(store, "withdraw", "WITHDRAW", 100)
    service = costs(store)
    for source in ["buy", "withdraw", "missing"]:
        with pytest.raises(Conflict):
            service.allocate(7, "order", 2, source, 1)
    with pytest.raises(Conflict):
        service.set_cost(8, "order", 2, "cost", 1)
    with pytest.raises(Conflict):
        service.set_cost(7, "order", 1, "cost", 1)
    source = store.rows("SELECT * FROM account_transactions WHERE id='premium'")[0]
    expense(store, value=25)
    with pytest.raises(Conflict, match="изменился"):
        service.allocate(7, "order", 2, "premium", 5, expected_source=source_fingerprint(source))
    value = service.view(7, "order")
    service.set_cost(7, "order", 2, "cost", 60)
    with pytest.raises(Conflict):
        service.review(7, "order", 2, value["fingerprint"])
    assert not store.rows("SELECT * FROM order_expense_allocations")


def test_profit_period_uses_confirmed_receipt_and_excludes_incomplete_and_foreign_orders(store):
    sale(store)
    receipt(store)
    review(costs(store))
    sale(store, "incomplete")
    receipt(store, "r2", "incomplete")
    sale(store, "undated")
    sale(store, "foreign", seller="other")
    report = profit_statistics(store, 7, "seller", 1, AT)
    assert report == {
        "profit": 40,
        "reviewed_orders": 1,
        "incomplete_orders": 1,
        "unattributed_orders": 1,
        "foreign_orders": 1,
    }
    with pytest.raises(Conflict):
        profit_statistics(store, 8, "seller", 1, AT)
    assert (
        profit_statistics(store, 7, "seller", 1, datetime(2026, 10, 20, tzinfo=UTC))["reviewed_orders"] == 0
    )


async def test_dispatcher_cost_allocation_review_restart_double_commit_without_api(store):
    sale(store)
    receipt(store)
    expense(store)
    api, session = CatalogAPI(), TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("pf:edit:cost:order")
        await ui.message("50,00")
        nonce = (await state.get_data())["profit_nonce"]
        # Real SQLite FSM reconstructs the pending confirmation after restart.
        await dp.storage.close()
        dp = dispatcher(store, api)
        ui = Interaction(bot, dp)
        await ui.click("pf:commit:" + nonce)
        await ui.click("pf:commit:" + nonce)
        assert "устарело" in session.calls[-1].text
        await ui.click("pf:edit:other_expenses:order")
        await ui.message("0")
        await ui.click("pf:commit:" + (await state.get_data())["profit_nonce"])
        await ui.click("pf:sources:order:0")
        nonce = (await state.get_data())["profit_nonce"]
        await ui.click(f"pf:pick:{nonce}:0")
        await ui.message("10")
        await ui.click("pf:commit:" + (await state.get_data())["profit_nonce"])
        await ui.click("pf:review:order")
        assert "Расчёт после указанных расходов: 30.00 ₽" in session.calls[-1].text
        await ui.click("pf:commit:" + (await state.get_data())["profit_nonce"])
        assert "Прибыль по учёту владельца: 30.00 ₽" in session.calls[-1].text
        await ui.click("sd:0:profit")
        assert "Итог только по ним: 30.00 ₽" in session.calls[-1].text
        assert not api.calls
        assert not store.rows("SELECT * FROM outbox")
        assert not store.rows("SELECT * FROM financial_operations")
        assert all(
            len(button.callback_data.encode()) <= 64
            for method in session.calls
            for row in (
                getattr(method, "reply_markup", None).inline_keyboard
                if getattr(method, "reply_markup", None)
                else []
            )
            for button in row
            if button.callback_data
        )
    finally:
        await dp.storage.close()
        await bot.session.close()
