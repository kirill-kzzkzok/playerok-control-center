"""A later confirmed server date can recover enrollment, never local receive time."""

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


@pytest.mark.parametrize("date", ["fresh", "2020-01-01T00:00:00Z"])
async def test_later_server_timestamp_recovers_only_verified_future_sale(shop, date):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-unit")
    full = r.api.order("late-date")
    short = {**full, "created_at": None}
    Orders(r.store).ingest(short)
    enrollment = EnrollmentWorker(r.store, r.api)
    assert await enrollment.once() == 0
    assert not r.store.rows("SELECT * FROM delivery_chains")
    if date != "fresh":
        full["created_at"] = date
    Orders(r.store).ingest(full)
    for _ in range(8):
        await enrollment.once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    chains = r.store.rows("SELECT status FROM delivery_chains")
    assert chains == ([{"status": "SUCCEEDED"}] if date == "fresh" else [])
    assert len(r.api.messages) == (2 if date == "fresh" else 0)
    # Duplicate events/restart cannot register another chain or send twice.
    Orders(r.store).ingest(full)
    await EnrollmentWorker(r.store, r.api).once()
    assert len(r.store.rows("SELECT * FROM delivery_chains")) == int(date == "fresh")


async def test_known_historical_timestamp_cannot_be_rewritten_as_future_authority(shop):
    from app.database.store import Conflict, now

    r = await shop()
    await setup_ui(r, "fixed_text", "private-unit")
    order = r.api.order("date-conflict")
    order["created_at"] = "2020-01-01T00:00:00Z"
    Orders(r.store).ingest(order)
    assert await EnrollmentWorker(r.store, r.api).once() == 0
    order["created_at"] = now()
    with pytest.raises(Conflict):
        Orders(r.store).ingest(order)
    assert not r.store.rows("SELECT * FROM delivery_chains")
    assert r.store.rows("SELECT status FROM auto_enrollment") == [{"status": "SKIPPED"}]
