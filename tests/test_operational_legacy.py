"""Actual old gift projections remain visible and quarantined after startup."""

import pytest

from app.database.store import now
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.services.product_readiness import ProductReadiness
from app.telegram.home_panel import HomePanel
from tests.test_gifts import enroll
from tests.test_gifts import rig as rig_fixture

rig = rig_fixture


@pytest.mark.parametrize("payment", ["pending", "unknown", "delivered"])
async def test_legacy_upgrade_preserves_jobs_and_blocks_new_payments(rig, payment):
    r = rig
    r.store.set_setting("playerok:account_id", r.api.account.id)
    await enroll(r)
    if payment != "pending":
        if payment == "unknown":
            r.remote.pay_error = TimeoutError()
        await r.worker.once()
    before = {
        table: r.store.rows("SELECT * FROM " + table)
        for table in ("gift_jobs", "gift_steps", "gift_listings", "outbox")
    }
    payments = len(r.remote.payments)
    logical = LogicalProducts(r.store, r.api)
    assert logical.migrate_legacy() == 1
    product = logical.listing("item")
    assert product["state"] == "LEGACY" and not product["enabled"]
    assert logical.migrate_legacy() == 0
    assert logical.listing("item")["id"] == product["id"]
    for table, rows in before.items():
        assert r.store.rows("SELECT * FROM " + table) == rows
    order = r.api.add_order("after-upgrade")
    order["created_at"] = now()
    Orders(r.store).ingest(order)
    assert r.service.discover() == 0
    await r.worker.once()
    assert len(r.remote.payments) == payments
    assert not r.store.rows("SELECT * FROM gift_jobs WHERE order_id='after-upgrade'")
    state = ProductReadiness(r.store, r.api, r.api.gift_sender).snapshot("item", item=r.api.item)
    assert not state["ready"]
    migration = next(p for p in state["problems"] if p["code"] == "CODE_MIGRATION")
    assert migration["action"][1] == "lp:item:item"
    attention = HomePanel(r.store, r.api).paid_issues()
    assert any(row["id"] == "after-upgrade" for row in attention)
