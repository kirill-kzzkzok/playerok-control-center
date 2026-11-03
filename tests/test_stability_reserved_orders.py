"""Paid orders retain immutable reservations across admission-only stock resume."""

import json

import pytest

from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_universal_delivery_e2e import simulation as simulation_fixture
from tests.test_ux_redesign import buttons


@pytest.fixture
async def shop(tmp_path):
    async for create in simulation_fixture.__wrapped__(tmp_path):
        yield create


@pytest.mark.parametrize("already_queued", [False, True])
@pytest.mark.parametrize("saved_fence", [False, True])
async def test_stock_resume_keeps_reserved_orders_deliverable(shop, already_queued, saved_fence):
    r = await shop()
    await setup_ui(r, "unique_stock")
    for i in range(3):
        Orders(r.store).ingest(r.api.order(f"reserved-{i}"))
    await EnrollmentWorker(r.store, r.api).once()
    assert len(r.store.rows("SELECT * FROM reservations")) == 3
    assert r.store.setting("delivery:lot:enabled") is False
    before = r.store.rows("SELECT * FROM reservations ORDER BY order_id")
    if not saved_fence:
        # Persisted shape from the previous version, not a fabricated final state.
        for row in r.store.rows("SELECT order_id,policy FROM delivery_chains"):
            policy = json.loads(row["policy"])
            policy.pop("_logical_authority", None)
            r.store.execute(
                "UPDATE delivery_chains SET policy=? WHERE order_id=?", (json.dumps(policy), row["order_id"])
            )
    if already_queued:
        ChainWorker(r.store, r.api).once()
    await r.ui.click("stock:view:lot")
    await click(r, action="stock:add:lot")
    await r.ui.message("new-private-unit")
    commit = next(k for k in buttons(r.session.last_panel) if k.startswith("stock:commit:"))
    await click(r, action=commit)
    await click(r, action="stock:resume:lot")
    confirm = next(k for k in buttons(r.session.last_panel) if k.startswith("stock:resumeok:"))
    await click(r, action=confirm)
    for _ in range(12):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert all(row["status"] == "SUCCEEDED" for row in r.store.rows("SELECT status FROM delivery_chains"))
    delivered = [m["text"] for m in r.api.messages if m["text"].startswith("isolated-unit-")]
    assert len(delivered) == 3 and len(set(delivered)) == 3
    assert r.store.rows("SELECT * FROM reservations ORDER BY order_id") == before
    assert r.store.rows("SELECT status FROM inventory WHERE secret='new-private-unit'") == [
        {"status": "AVAILABLE"}
    ]
    assert not await r.worker.once()


@pytest.mark.parametrize("saved_fence", [False, True])
async def test_owner_stop_and_rearm_do_not_revive_old_reserved_authority(shop, saved_fence):
    r = await shop()
    await setup_ui(r, "unique_stock")
    Orders(r.store).ingest(r.api.order("reserved-before-stop"))
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    reservations = r.store.rows("SELECT * FROM reservations")
    assert len(reservations) == 1
    if not saved_fence:
        row = r.store.rows("SELECT policy FROM delivery_chains")[0]
        policy = json.loads(row["policy"])
        policy.pop("_logical_authority", None)
        r.store.execute("UPDATE delivery_chains SET policy=?", (json.dumps(policy),))
    logical = LogicalProducts(r.store, r.api)
    product = logical.listing("lot")
    logical.stop(7, product["id"])
    await logical.arm(7, product["id"], product["revision"], inherit=True)
    for _ in range(4):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert not r.api.messages
    assert r.store.rows("SELECT * FROM reservations") == reservations
    assert not r.store.rows("SELECT 1 FROM inventory WHERE status='DELIVERED'")
