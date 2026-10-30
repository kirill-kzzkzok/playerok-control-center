from datetime import UTC, datetime

import pytest
from aiogram import Bot

from app.database.store import Conflict, Store
from app.services.order_finance import expense_capacity, settlement_statistics
from app.services.settlements import sale_settlement, withdrawal_settlement
from app.services.transactions import transaction_statistics
from tests.test_order_finance import costs, expense, receipt, review, sale
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher

AT = datetime(2026, 10, 29, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "settlements.sqlite")
    s.claim_first_owner(7)
    s.set_setting("playerok:account_id", "seller")
    s.set_setting("transactions:account_id", "seller")
    yield s
    s.close()


def transaction(**changes):
    return {
        "operation": "SELL",
        "direction": "IN",
        "status": "CONFIRMED",
        "currency": "RUB",
        "completed_at": AT.isoformat(),
        "value": "90",
        "reported_fee": "10",
        **changes,
    }


def test_sale_fee_is_confirmed_only_by_exact_observed_equation():
    value = sale_settlement("100", transaction())
    assert value["reconciled"] and value["fee"] == 10 and value["credit"] == 90
    value = sale_settlement("100", transaction(value="100", reported_fee="0"))
    assert value["reconciled"] and value["fee"] == 0
    # 20 percent and 50 RUB are different units: never multiply or infer a rate.
    assert not sale_settlement("250", transaction(value="200", reported_fee="20"))["reconciled"]
    assert sale_settlement("250", transaction(value="200", reported_fee="50"))["reconciled"]


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": "WITHDRAW"},
        {"direction": "OUT"},
        {"status": "ROLLED_BACK"},
        {"currency": "USD"},
        {"completed_at": None},
        {"completed_at": "2026-10-29T12:00:00"},
        {"reported_fee": None},
        {"reported_fee": "NaN"},
        {"reported_fee": "-1"},
        {"value": "120"},
        {"value": None},
    ],
)
def test_incomplete_or_unmatched_transaction_is_never_a_confirmed_sale_fee(changes):
    value = sale_settlement("100", transaction(**changes))
    assert not value["reconciled"] and value["fee"] is None and value["reason"]


def test_period_aggregate_uses_linked_orders_and_ignores_cost_unknowns_and_other_operations(store):
    sale(store)
    receipt(store, fee=10)
    sale(store, "missing_fee")
    receipt(store, "r2", "missing_fee", fee=None)
    sale(store, "unlinked")
    expense(store, value=500, fee=999)
    report = settlement_statistics(store, 7, "seller", 1, AT)
    assert report == {
        "orders": 1,
        "gross": 100,
        "credit": 90,
        "fees": 10,
        "unreconciled": 1,
        "unattributed": 1,
        "foreign": 0,
    }
    with pytest.raises(Conflict):
        settlement_statistics(store, 8, "seller", 1, AT)
    assert settlement_statistics(store, 7, "seller", 1, AT.replace(year=2025))["orders"] == 0


def test_commission_already_in_credit_is_not_subtracted_twice_and_fee_change_revokes_review(store):
    sale(store)
    receipt(store, fee=10)
    service = costs(store, cost=50)
    value = review(service)
    assert value["profit"] == 40
    assert value["settlement"]["fee"] == 10
    receipt(store, fee=20)
    value = service.view(7, "order")
    assert value["profit"] is None
    assert value["candidate_profit"] == 40
    assert not value["settlement"]["reconciled"]
    assert settlement_statistics(store, 7, "seller", 0, AT)["fees"] == 0


def test_duplicate_receipt_or_refunded_order_is_not_counted_in_fee_totals(store):
    sale(store)
    receipt(store, fee=10)
    receipt(store, "second", fee=10)
    assert settlement_statistics(store, 7, "seller", 0, AT)["fees"] == 0
    receipt(store, "second", status="ROLLED_BACK")
    assert settlement_statistics(store, 7, "seller", 0, AT)["fees"] == 10
    sale(store, status="ROLLED_BACK")
    assert settlement_statistics(store, 7, "seller", 0, AT)["fees"] == 0


def test_withdrawal_retention_requires_positive_explicit_net_and_no_rate_fallback(store):
    for ident, net in [("known", 94), ("zero", 0), ("absent", None), ("impossible", 110)]:
        expense(store, ident, "WITHDRAW", 100, props={"amount_after_conversion_rub": net}, fee=0)
    stats = transaction_statistics(store, 1, AT)
    assert stats["withdraw_retained_known"] == 6
    assert stats["withdraw_known_count"] == 1
    assert stats["withdraw_net_reported"] == 94
    assert stats["withdraw_net_unknown"] == 3
    rows = {row["id"]: row for row in store.rows("SELECT * FROM account_transactions")}
    assert expense_capacity(rows["known"]) == 6
    assert expense_capacity(rows["zero"]) is None
    assert withdrawal_settlement({**rows["known"], "currency": "USD"}) is None


async def test_dispatcher_exposes_reconciled_fees_and_explicit_coverage_without_api(store, monkeypatch):
    sale(store)
    receipt(store, fee=10)
    monkeypatch.setattr(
        "app.telegram.statistics_panel.settlement_statistics",
        lambda s, owner, account, days: settlement_statistics(s, owner, account, days, AT),
    )
    session, api = TelegramSession(), CatalogAPI()
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        await ui.click("sd:7:settlements")
        text = session.calls[-1].text
        assert "Сверенная комиссия продажи: 10.00 ₽" in text
        assert "повторно не вычитается" in text
        assert "Однозначно сверено заказов: 1" in text
        await ui.click("pf:card:order")
        assert "сверенная комиссия продажи: 10.00 ₽" in session.calls[-1].text
        assert not api.calls
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await dp.storage.close()
        await bot.session.close()
