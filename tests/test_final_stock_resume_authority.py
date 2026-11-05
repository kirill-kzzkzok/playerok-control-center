"""Admission resume cannot override logical STOP or revive old reserved sends."""

from copy import deepcopy

from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.logical_products import LogicalProductWorker
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_stock_resume_confirmation_cannot_override_a_later_logical_stop(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    with r.store.transaction() as db:
        Orders.empty_stock(db, "lot", "no-order")
    # A legacy persisted pause must still require explicit safe resume.
    r.store.set_setting("delivery:lot:enabled", False)
    await r.ui.click("stock:resume:lot")
    confirmation = next(key for key in buttons(r.session.last_panel) if key.startswith("stock:resumeok:"))
    logical = LogicalProducts(r.store, r.api)
    product = logical.listing("lot")
    logical.stop(7, product["id"])
    await r.ui.click(confirmation)
    assert r.store.setting("delivery:lot:enabled") is False
    assert not logical.listing("lot")["enabled"]
    assert not r.api.messages and not r.api.remote.payments


async def test_stock_resume_cannot_ignore_changed_logical_identity(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    with r.store.transaction() as db:
        Orders.empty_stock(db, "lot", "no-order")
    # A legacy persisted pause must still require explicit safe resume.
    r.store.set_setting("delivery:lot:enabled", False)
    await r.ui.click("stock:resume:lot")
    confirmation = next(key for key in buttons(r.session.last_panel) if key.startswith("stock:resumeok:"))
    r.api.item["attributes"] = {"delivery-critical": "changed by another client"}
    await r.ui.click(confirmation)
    assert r.store.setting("delivery:lot:enabled") is False
    assert not r.api.messages and not r.api.remote.payments


async def test_relisted_stock_resume_keeps_reserved_orders_and_logical_authority(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    r.api.item["status"] = "SOLD"
    r.api.items["next-lot"] = {**deepcopy(r.api.item), "id": "next-lot", "status": "APPROVED"}
    await LogicalProductWorker(r.store, r.api).once()
    logical = LogicalProducts(r.store, r.api)
    before = logical.listing("next-lot")
    assert before["id"] == logical.listing("lot")["id"]
    for i in range(3):
        Orders(r.store).ingest(r.api.order(f"reserved-{i}", "next-lot"))
    await EnrollmentWorker(r.store, r.api).once()
    reservations = r.store.rows("SELECT * FROM reservations ORDER BY order_id")
    # Backward compatibility with a pause persisted before automatic restocking.
    r.store.set_setting("delivery:next-lot:enabled", False)
    r.store.set_setting("delivery:lot:enabled", False)
    assert len(reservations) == 3 and r.store.setting("delivery:next-lot:enabled") is False
    await r.ui.click("stock:add:next-lot")
    await r.ui.message("one-new-private-unit")
    await r.ui.click(next(key for key in buttons(r.session.last_panel) if key.startswith("stock:commit:")))
    await r.ui.click("stock:resume:next-lot")
    confirm = next(key for key in buttons(r.session.last_panel) if key.startswith("stock:resumeok:"))
    await r.ui.click(confirm)
    assert r.store.setting("delivery:next-lot:enabled") is True
    assert r.store.setting("delivery:lot:enabled") is False
    after = logical.listing("next-lot")
    assert all(
        after[key] == before[key] for key in ("id", "code", "control_revision", "armed_at", "inherit_enabled")
    )
    policies = r.store.rows("SELECT * FROM delivery_policies ORDER BY item_id")
    await r.ui.click(confirm)
    assert r.store.rows("SELECT * FROM delivery_policies ORDER BY item_id") == policies
    for _ in range(12):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert r.store.rows("SELECT * FROM reservations ORDER BY order_id") == reservations
    assert all(row["status"] == "SUCCEEDED" for row in r.store.rows("SELECT status FROM delivery_chains"))
    assert len([message for message in r.api.messages if message["text"].startswith("isolated-unit-")]) == 3
    assert not r.api.remote.payments
