"""Actual old gift projections remain visible and quarantined after startup."""

import pytest

from app.database.store import now
from app.gifts.worker import GiftWorker
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.services.product_readiness import ProductReadiness
from app.telegram.home_panel import HomePanel
from tests.test_gift_bundles import setup as setup_bundle
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


async def test_legacy_upgrade_does_not_reset_or_refund_partially_delivered_bundle(rig):
    r = rig
    r.store.set_setting("playerok:account_id", r.api.account.id)
    remote = await setup_bundle(r, "bear, rose, heart", auto=True)
    await r.worker.once()
    first = r.service.steps("order")[0]
    assert first["state"] == "SUCCEEDED" and first["receipt"]
    logical = LogicalProducts(r.store, r.api)
    assert logical.migrate_legacy() == 1
    assert logical.migrate_legacy() == 0
    await r.worker.once()
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    steps = r.service.steps("order")
    assert steps[0] == first
    assert steps[1]["state"] == "BLOCKED" and steps[2]["state"] == "PENDING"
    assert len(remote.payments) == 1
    assert not r.api.refunds and not r.api.sent
    assert not r.store.rows("SELECT * FROM outbox WHERE kind IN ('fulfill','gift_refund','gift_notice')")
