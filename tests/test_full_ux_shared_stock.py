from app.services.inventory import Inventory
from app.services.orders import Orders
from tests.test_product_readiness import shop as shop_fixture
from tests.test_unified_delivery_controls import state
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_order_stock_shortcut_uses_the_same_product_importer_and_logical_pool(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    Orders(r.store).ingest(r.api.order("paid"))
    before = Inventory(r.store, r.api).counts("lot")["AVAILABLE"]
    await r.ui.click("order:paid")
    await r.ui.click("od:stock:paid")
    assert await state(r).get_state() == "stock:input"
    await r.ui.message("new-unit-one\nnew-unit-two")
    commit = next(k for k in buttons(r.session.last_panel) if k.startswith("stock:commit:"))
    await r.ui.click(commit)
    await r.ui.click(commit)
    assert Inventory(r.store, r.api).counts("lot")["AVAILABLE"] == before + 2
    scope = Inventory(r.store, r.api).scope("lot")
    assert r.store.rows("SELECT item_id FROM inventory WHERE secret='new-unit-one'") == [{"item_id": scope}]
    assert not r.api.messages and not r.api.remote.payments
    assert not r.store.rows("SELECT * FROM reservations")
