from copy import deepcopy
from datetime import UTC, datetime, timedelta

from app.database.store import Store
from app.services.orders import Orders
from tests.test_finance_panel import FinancialAPI
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


async def test_product_cost_snapshots_only_future_orders_and_survives_change_and_restart(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    original = r.api.call
    boundary = FinancialAPI()
    boundary.item = {**deepcopy(r.api.item), "fee_multiplier": "0.1", "raw_price": 100}
    r.api.call = boundary.call
    await r.ui.click("fin:cost:lot")
    await r.ui.message("25")
    r.api.call = original
    first = r.api.order("first")
    Orders(r.store).ingest(first)
    row = r.store.rows("SELECT * FROM order_costs WHERE order_id='first'")[0]
    assert row["cost"] == "25.00" and row["other_expenses"] is None
    assert row["review_fingerprint"] is None
    r.api.call = boundary.call
    await r.ui.click("fin:cost:lot")
    await r.ui.message("30")
    r.api.call = original
    Orders(r.store).ingest(first)
    Orders(r.store).ingest(r.api.order("second"))
    old = r.api.order("historical")
    old["created_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    Orders(r.store).ingest(old)
    unknown = r.api.order("undated")
    unknown["created_at"] = None
    Orders(r.store).ingest(unknown)
    assert r.store.rows("SELECT order_id,cost FROM order_costs ORDER BY order_id") == [
        {"order_id": "first", "cost": "25.00"},
        {"order_id": "second", "cost": "30.00"},
    ]
    path = r.store.rows("PRAGMA database_list")[0]["file"]
    reopened = Store(path)
    try:
        Orders(reopened).ingest(first)
        assert reopened.rows("SELECT cost FROM order_costs WHERE order_id='first'") == [{"cost": "25.00"}]
    finally:
        reopened.close()
    from app.workers.logical_products import LogicalProductWorker

    r.api.item["status"] = "SOLD"
    r.api.items["second-listing"] = {**deepcopy(r.api.item), "id": "second-listing", "status": "APPROVED"}
    await LogicalProductWorker(r.store, r.api).once()
    Orders(r.store).ingest(r.api.order("relisted-sale", item_id="second-listing"))
    assert r.store.rows("SELECT cost FROM order_costs WHERE order_id='relisted-sale'") == [{"cost": "30.00"}]
    assert not r.api.messages and not r.api.remote.payments


async def test_completed_sales_use_only_dated_owner_declarations(shop):
    from tests.test_ux_redesign import buttons

    r = await shop()
    await setup_ui(r, "unique_stock")
    original = r.api.call
    boundary = FinancialAPI()
    boundary.item = {**deepcopy(r.api.item), "fee_multiplier": "0.1", "raw_price": 100}
    r.api.call = boundary.call
    await r.ui.click("fin:cost:lot")
    await r.ui.message("25")
    before_zero = r.api.order("before-zero", status="CONFIRMED")
    await r.ui.click("fin:options:lot")
    assert "fin:otherzero:lot" in buttons(r.session.last_panel)
    await r.ui.click("fin:otherzero:lot")
    Orders(r.store).ingest(before_zero)
    for i in range(100):
        if i == 50:
            await r.ui.click("fin:cost:lot")
            await r.ui.message("30")
        order = r.api.order(f"sale-{i:03}", status="CONFIRMED")
        Orders(r.store).ingest(order)
        Orders(r.store).ingest(order)
    rows = r.store.rows("SELECT cost,other_expenses FROM order_costs ORDER BY order_id")
    assert rows == (
        [{"cost": "25.00", "other_expenses": None}]
        + [{"cost": "25.00", "other_expenses": "0.00"}] * 50
        + [{"cost": "30.00", "other_expenses": "0.00"}] * 50
    )
    r.api.call = original
    assert not r.api.messages and not r.api.remote.payments


async def test_saved_profit_view_opens_without_playerok_reads(shop):
    from app.playerok.adapter import APIError

    r = await shop()
    await setup_ui(r, "unique_stock")
    boundary = FinancialAPI()
    boundary.item = {**deepcopy(r.api.item), "fee_multiplier": "0.1", "raw_price": 100}
    r.api.call = boundary.call
    await r.ui.click("finance:lot")
    assert "Прогноз будущей продажи" in r.session.last_panel.text
    calls = []

    async def offline(method, **kwargs):
        calls.append(method)
        raise APIError("NETWORK_ERROR")

    r.api.call = offline
    await r.ui.click("finance:lot")
    assert "Прогноз будущей продажи" in r.session.last_panel.text
    assert "Снимок расчёта" in r.session.last_panel.text
    assert not calls
    from app.services.financial_quotes import refresh_due_quote

    await r.ui.click("fin:itemrefresh:lot")
    await refresh_due_quote(r.store, r.api)
    assert calls == ["get_item"]
    queued = r.store.rows("SELECT key FROM settings WHERE key GLOB 'ui:finance_refresh:*'")
    assert len(queued) == 1
    r.store.set_setting(queued[0]["key"], {"due": 0, "owner": 7, "item": "lot", "account": "seller"})
    boundary.item.update(price=120, raw_price=120)
    r.api.call = boundary.call
    await refresh_due_quote(r.store, r.api)
    assert not r.store.setting(queued[0]["key"])
    await r.ui.click("finance:lot")
    assert "120.00 ₽" in r.session.last_panel.text
