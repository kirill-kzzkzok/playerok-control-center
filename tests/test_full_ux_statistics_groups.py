from copy import deepcopy

from app.services.orders import Orders
from app.services.statistics import grouped_statistics, offer_suggestions
from app.workers.logical_products import LogicalProductWorker
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


async def test_verified_native_relist_is_one_statistical_product_without_manual_group(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    Orders(r.store).ingest(r.api.order("old-sale", status="CONFIRMED"))
    r.api.item["status"] = "SOLD"
    r.api.items["second"] = {**deepcopy(r.api.item), "id": "second", "status": "APPROVED"}
    await LogicalProductWorker(r.store, r.api).once()
    assert len(r.store.rows("SELECT * FROM logical_listings WHERE verified_at IS NOT NULL")) == 2
    Orders(r.store).ingest(r.api.order("new-sale", item_id="second", status="CONFIRMED"))
    before = r.store.rows("SELECT * FROM orders ORDER BY id")
    result = grouped_statistics(r.store, 7, "seller", 0)
    assert list(result["item_orders"].values()) == [2]
    assert sorted(next(iter(result["offer_sources"].values()))) == ["lot", "second"]
    assert not offer_suggestions(r.store, 7, "seller", 0)
    await r.ui.click("sd:0:items")
    assert r.session.last_panel.text.count("Real digital product") == 1
    assert "200.00 ₽" in r.session.last_panel.text
    assert r.store.rows("SELECT * FROM orders ORDER BY id") == before
    assert not r.store.setting("analytics:7:seller:offer_groups") and not r.api.remote.payments
