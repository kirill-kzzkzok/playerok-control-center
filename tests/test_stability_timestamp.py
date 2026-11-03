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
    from app.database.store import now, server_time

    r = await shop()
    await setup_ui(r, "fixed_text", "private-unit")
    order = r.api.order("date-conflict")
    order["created_at"] = "2020-01-01T00:00:00Z"
    Orders(r.store).ingest(order)
    assert await EnrollmentWorker(r.store, r.api).once() == 0
    order["created_at"] = now()
    Orders(r.store).ingest(order)
    assert await EnrollmentWorker(r.store, r.api).once() == 0
    payload = r.store.rows("SELECT json_extract(payload,'$.created_at') AS date FROM orders")[0]["date"]
    assert server_time(payload) == server_time("2020-01-01T00:00:00Z")
    assert not r.store.rows("SELECT * FROM delivery_chains")
    assert r.store.rows("SELECT status FROM auto_enrollment") == [{"status": "SKIPPED"}]


async def test_coarse_server_time_is_not_lifted_over_original_owner_cutoff(shop):
    from datetime import datetime

    from app.database.store import server_time

    r = await shop()
    await setup_ui(r, "fixed_text", "private-unit")
    cutoff = datetime.fromisoformat(r.store.rows("SELECT updated_at FROM delivery_policies")[0]["updated_at"])
    coarse = cutoff.replace(microsecond=0).isoformat()
    order = r.api.order("coarse-date")
    order["created_at"] = coarse
    Orders(r.store).ingest(order)
    assert await EnrollmentWorker(r.store, r.api).once() == 0
    order["created_at"] = cutoff.replace(microsecond=999999).isoformat()
    Orders(r.store).ingest(order)
    assert await EnrollmentWorker(r.store, r.api).once() == 0
    assert not r.store.rows("SELECT * FROM delivery_chains")
    payload = r.store.rows("SELECT json_extract(payload,'$.created_at') AS date FROM orders")[0]["date"]
    assert server_time(payload) == server_time(coarse)
