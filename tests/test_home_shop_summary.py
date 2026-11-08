"""Existing finance service, worker boundary and locally rendered shop summary."""

from types import SimpleNamespace

from app.database.store import now
from app.playerok.adapter import APIError
from app.telegram.home_panel import HomePanel
from app.workers.transactions import TransactionWorker
from tests.test_order_finance import costs, receipt, review, sale
from tests.test_order_finance import store as store_fixture

store = store_fixture


async def test_transaction_worker_caches_confirmed_profit_without_home_network_reads(store):
    sale(store)
    receipt(store, completed_at=now())
    review(costs(store))
    reads = []

    class Boundary:
        account = SimpleNamespace(id="seller")

        async def call(self, method, **kwargs):
            reads.append(method)
            assert method == "get_transactions"
            return {"transactions": [], "page_info": {"has_next_page": False}}

    api = Boundary()
    await TransactionWorker(store, api).once()
    assert reads == ["get_transactions"]
    store.set_setting("gifts:balance", {"stars": 135, "at": now()})
    text, _ = HomePanel(store, api).home(owner=7)
    assert "40.00 ₽" in text
    assert "135 ⭐" in text
    assert "Оплаченные заказы:" in text
    assert reads == ["get_transactions"]
    assert not store.rows("SELECT * FROM outbox")


async def test_failed_refresh_keeps_stale_evidence_unconfirmed_in_home(store):
    sale(store)
    receipt(store)
    review(costs(store))

    class Boundary:
        account = SimpleNamespace(id="seller")
        fail = False

        async def call(self, method, **kwargs):
            if self.fail:
                raise APIError("NETWORK_ERROR")
            return {"transactions": [], "page_info": {"has_next_page": False}}

    api = Boundary()
    worker = TransactionWorker(store, api)
    await worker.once()
    key = "shop:profit:seller:7"
    saved = store.setting(key)
    saved["at"] = "2000-01-01T00:00:00+00:00"
    store.set_setting(key, saved)
    store.set_setting("gifts:balance", {"stars": 135, "at": saved["at"]})
    store.set_setting("ui:catalog:seller", {"complete": True, "at": saved["at"], "items": []})
    api.fail = True
    await worker.once()
    assert store.setting(key) == saved
    text, _ = HomePanel(store, api).home(owner=7)
    assert "Прибыль: ожидаем" in text
    assert "135 ⭐" not in text
    assert "Активные товары: обновляем" in text
    assert worker.health["status"] == "ERROR"


async def test_incomplete_costs_are_not_reported_as_zero_profit(store):
    sale(store)
    receipt(store, completed_at=now())

    class Boundary:
        account = SimpleNamespace(id="seller")

        async def call(self, method, **kwargs):
            return {"transactions": [], "page_info": {"has_next_page": False}}

    api = Boundary()
    await TransactionWorker(store, api).once()
    text, _ = HomePanel(store, api).home(owner=7)
    assert "Себестоимость неизвестна для 1 зачислений" in text
    assert "Прибыль: 0" not in text
    foreign = SimpleNamespace(account=SimpleNamespace(id="other"))
    text, _ = HomePanel(store, foreign).home(owner=7)
    assert "Прибыль: ожидаем" in text
    assert not store.rows("SELECT * FROM outbox")


async def test_home_shows_store_result_with_known_expenses_without_order_review(store):
    from tests.test_order_finance import expense

    sale(store)
    receipt(store, completed_at=now())
    costs(store, cost=40, other=3)
    expense(store, "bump", value=20, completed_at=now())
    expense(store, "publish", "ITEM_CUSTOM_PRIORITY", 5, completed_at=now())
    expense(store, "withdraw", "WITHDRAW", 100, props={"amount_after_conversion_rub": 94}, completed_at=now())
    expense(store, "refund-premium", value=50, status="ROLLED_BACK", completed_at=now())
    expense(store, "old", value=70)

    async def call(method, **kwargs):
        assert method == "get_transactions"
        return {"transactions": [], "page_info": {"has_next_page": False}}

    api = SimpleNamespace(account=SimpleNamespace(id="seller"), call=call)
    await TransactionWorker(store, api).once()
    text, _ = HomePanel(store, api).home(owner=7)
    assert "Зачисления за продажи: 90.00 ₽" in text
    assert "Себестоимость: 40.00 ₽" in text
    assert "PREMIUM и размещение: 25.00 ₽" in text
    assert "Итог по известным данным: 16.00 ₽" in text


def test_daily_profit_deduplicates_premium_receipt_and_excludes_foreign_account(store):
    from app.database.store import encode
    from app.services.order_finance import cache_shop_profit
    from tests.test_order_finance import expense

    sale(store)
    receipt(store, completed_at=now())
    costs(store, cost=40, other=0)
    for key, account, status, payment in (
        ("known", "seller", "SUCCEEDED", "bump"),
        ("local", "seller", "SUCCEEDED", "receipt-local"),
        ("duplicate", "seller", "SUCCEEDED", "receipt-local"),
        ("unknown", "seller", "UNCERTAIN", "unknown"),
        ("foreign", "other", "SUCCEEDED", "receipt-foreign"),
    ):
        op = store.enqueue(key, "premium_bump", key + "-product", {"account_id": account})
        store.execute(
            "UPDATE outbox SET status=?,result=? WHERE id=?",
            (status, encode({"payment_id": payment, "paid_rub": "9.00"}), op),
        )
        store.execute(
            "INSERT INTO promotion_expenses VALUES(?,?,?, ?,?)",
            (key, key + "-product", "9.00", status, now()),
        )
    expense(store, "bump", value=9, completed_at=now())
    cache_shop_profit(store, "seller")
    result = store.setting("shop:profit:seller:7")
    assert result["promotion"] == "18.00"
    assert result["net"] == "32.00"
    assert result["missing_cost"] == 0
    assert result["uncertain_promotions"] == 1
    expense(store, "receipt-local", value=9, completed_at=now())
    cache_shop_profit(store, "seller")
    assert store.setting("shop:profit:seller:7")["net"] == "32.00"


def test_daily_profit_excludes_old_sales_and_reports_missing_cost(store):
    from datetime import UTC, datetime, timedelta

    from app.services.order_finance import cache_shop_profit
    from tests.test_order_finance import expense

    sale(store)
    receipt(store, completed_at=(datetime.now(UTC) - timedelta(hours=25)).isoformat())
    costs(store, cost=40, other=0)
    sale(store, "today")
    receipt(store, "receipt-today", "today", value=80, completed_at=now())
    expense(store, "today-bump", value=9, completed_at=now())
    cache_shop_profit(store, "seller")
    result = store.setting("shop:profit:seller:7")
    assert result["sales_credit"] == "80.00"
    assert result["cost"] == "0.00"
    assert result["missing_cost"] == 1
    assert result["net"] == "71.00"


async def test_website_premium_is_counted_without_bot_operation_after_restart(store):
    from app.database.store import Store

    at = now()
    entries = [
        {
            "id": ident,
            "operation": operation,
            "direction": direction,
            "status": "CONFIRMED",
            "currency": "RUB",
            "value": value,
            "created_at": at,
            "completed_at": at,
            "user": {"id": "seller"},
        }
        for ident, operation, direction, value in (
            ("website-sale", "SELL", "IN", 90),
            ("website-premium", "ITEM_PREMIUM_PRIORITY", "OUT", 9),
            ("website-placement", "ITEM_CUSTOM_PRIORITY", "OUT", 5),
        )
    ]

    async def call(method, **kwargs):
        assert method == "get_transactions"
        return {"transactions": entries, "page_info": {"has_next_page": False}}

    api = SimpleNamespace(account=SimpleNamespace(id="seller"), call=call)
    await TransactionWorker(store, api).once()
    assert store.setting("shop:profit:seller:7")["net"] == "76.00"
    assert not store.rows("SELECT * FROM outbox")
    assert not store.rows("SELECT * FROM promotion_expenses")
    restarted = Store(store.path)
    try:
        await TransactionWorker(restarted, api).once()
        result = restarted.setting("shop:profit:seller:7")
        assert result["sales_credit"] == "90.00"
        assert result["promotion"] == "14.00"
        assert result["net"] == "76.00"
        assert result["missing_cost"] == 1
        text, _ = HomePanel(restarted, api).home(owner=7)
        assert "PREMIUM и размещение: 14.00 ₽" in text
        assert len(restarted.rows("SELECT id FROM account_transactions")) == 3
    finally:
        restarted.close()
