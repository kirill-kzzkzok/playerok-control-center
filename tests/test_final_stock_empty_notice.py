"""Empty-stock notices follow the shared stock bucket after relisting."""

from copy import deepcopy

import pytest

from app.services.orders import Orders
from app.workers.enrollment import EnrollmentWorker
from app.workers.logical_products import LogicalProductWorker
from app.workers.notifications import Notifications
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize("restock", [False, True])
async def test_stock_notice_uses_shared_scope_and_never_resumes_delivery(shop, restock):
    r = await shop()
    await setup_ui(r, "unique_stock")
    r.store.set_setting("notifications:initialized", True)
    r.api.item["status"] = "SOLD"
    r.api.items["next-lot"] = {**deepcopy(r.api.item), "id": "next-lot", "status": "APPROVED"}
    await LogicalProductWorker(r.store, r.api).once()
    for i in range(3):
        Orders(r.store).ingest(r.api.order(f"order-{i}", "next-lot"))
    await EnrollmentWorker(r.store, r.api).once()
    reservations = r.store.rows("SELECT * FROM reservations ORDER BY order_id")
    assert len(reservations) == 3
    if restock:
        await r.ui.click("stock:add:next-lot")
        await r.ui.message("new-private-stock")
        await r.ui.click(
            next(key for key in buttons(r.session.last_panel) if key.startswith("stock:commit:"))
        )
    await Notifications(r.store, r.bot).once()
    notices = r.store.rows(
        "SELECT n.status FROM notifications n JOIN inbox i ON i.key=n.event_key WHERE i.kind='stock_empty'"
    )
    assert notices == [{"status": "SKIPPED" if restock else "SUCCEEDED"}]
    assert r.store.setting("delivery:next-lot:enabled") is False
    assert r.store.rows("SELECT * FROM reservations ORDER BY order_id") == reservations
    assert not r.api.messages and not r.api.remote.payments
