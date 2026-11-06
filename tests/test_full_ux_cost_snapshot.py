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
