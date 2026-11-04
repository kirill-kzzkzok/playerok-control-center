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
    receipt(store)
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
    receipt(store)

    class Boundary:
        account = SimpleNamespace(id="seller")

        async def call(self, method, **kwargs):
            return {"transactions": [], "page_info": {"has_next_page": False}}

    api = Boundary()
    await TransactionWorker(store, api).once()
    text, _ = HomePanel(store, api).home(owner=7)
    assert "Прибыль: не хватает данных" in text
    assert "Прибыль: 0" not in text
    foreign = SimpleNamespace(account=SimpleNamespace(id="other"))
    text, _ = HomePanel(store, foreign).home(owner=7)
    assert "Прибыль: ожидаем" in text
    assert not store.rows("SELECT * FROM outbox")
