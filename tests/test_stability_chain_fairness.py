"""Waiting ACTIVE chains must not occupy the scheduler's first page forever."""

import pytest

from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_stability_deal_completeness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui


@pytest.fixture
async def shop(tmp_path):
    async for create in shop_fixture.__wrapped__(tmp_path):
        yield create


async def test_fifty_waiting_chains_do_not_starve_next_paid_order_after_restart(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-unit")
    enrollment = EnrollmentWorker(r.store, r.api)
    for index in range(50):
        Orders(r.store).ingest(r.api.order(f"a-waiting-{index:03}"))
    for _ in range(13):
        await enrollment.once()
    assert len(r.store.rows("SELECT * FROM delivery_chains")) == 50
    ChainWorker(r.store, r.api).once()  # all50 wait for their first external confirmation
    old = r.store.rows("SELECT id FROM outbox WHERE kind='delivery_text'")
    assert len(old) == 50
    Orders(r.store).ingest(r.api.order("z-new"))
    await enrollment.once()
    for _ in range(4):
        ChainWorker(r.store, r.api).once()
    ops = r.store.rows("SELECT id FROM outbox WHERE target='z-new' AND kind='delivery_text'")
    assert len(ops) == 1
    assert len(r.store.rows("SELECT id FROM outbox WHERE kind='delivery_text'")) == 51
    await r.worker.once(operation_id=ops[0]["id"])
    assert len(r.api.messages) == 1 and r.api.messages[0]["chat_id"] == "chat-z-new"
    assert not r.api.remote.payments
