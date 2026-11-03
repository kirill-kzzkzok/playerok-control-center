"""Fifty paid events exercise actual reservations, guards, workers and receipts."""

from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_fifty_paid_orders_deliver_distinct_stock_once_under_duplicate_events(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    await r.ui.click("stock:add:lot")
    await r.ui.message("\n".join("additional-secret-" + str(i) for i in range(47)))
    await r.ui.click(next(key for key in buttons(r.session.last_panel) if key.startswith("stock:commit:")))
    orders = Orders(r.store)
    for i in range(50):
        order = r.api.order("load-" + str(i))
        orders.ingest(order)
        orders.ingest(order)
    enrollment = EnrollmentWorker(r.store, r.api)
    for _ in range(13):
        await enrollment.once()
    assert len(r.store.rows("SELECT * FROM reservations")) == 50
    chains = ChainWorker(r.store, r.api)
    for _ in range(110):
        chains.once()
        await r.worker.once()
    assert r.store.rows("SELECT count(*) n FROM inventory WHERE status='DELIVERED'") == [{"n": 50}]
    assert r.store.rows("SELECT count(*) n FROM delivery_chains WHERE status='SUCCEEDED'") == [{"n": 50}]
    deliveries = [m for m in r.api.messages if m["text"] != "Спасибо за покупку!"]
    assert len(deliveries) == 50 and len({m["text"] for m in deliveries}) == 50
    assert len({m["chat_id"] for m in deliveries}) == 50
    assert not await r.worker.once()
    assert await enrollment.once() == 0
    assert not r.api.remote.payments
