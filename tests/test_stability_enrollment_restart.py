"""Exclusive startup lease recovery; old paid-order eligibility is not expanded."""

import pytest

from app.security.instance import InstanceLock
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_universal_delivery_e2e import simulation as simulation_fixture


@pytest.fixture
async def shop(tmp_path):
    async for create in simulation_fixture.__wrapped__(tmp_path):
        yield create


async def test_exclusive_startup_reclaims_unexpired_check_and_delivers_once(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "one-private-unit")
    Orders(r.store).ingest(r.api.order("interrupted-check"))
    previous = EnrollmentWorker(r.store, r.api)
    previous.discover()
    claimed = previous.claim()
    assert claimed and not previous.claim()
    restarted = EnrollmentWorker(r.store, r.api)
    with InstanceLock(str(r.store.path) + ".lock") as lock:
        assert restarted.recover_startup(lock) == 1
        assert restarted.recover_startup(lock) == 0
    await restarted.once()
    assert len(r.store.rows("SELECT * FROM delivery_chains")) == 1
    assert (
        r.store.execute(
            "UPDATE auto_enrollment SET status='BLOCKED' WHERE order_id=? AND lease_token=?",
            (claimed["order_id"], claimed["lease_token"]),
        ).rowcount
        == 0
    )
    for _ in range(6):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert [m["text"] for m in r.api.messages].count("one-private-unit") == 1
    assert not await r.worker.once()


async def test_recovery_requires_the_current_database_lock(shop, tmp_path):
    r = await shop()
    worker = EnrollmentWorker(r.store, r.api)
    with InstanceLock(tmp_path / "different.sqlite3.lock") as wrong, pytest.raises(RuntimeError):
        worker.recover_startup(wrong)
    right = InstanceLock(str(r.store.path) + ".lock")
    with pytest.raises(RuntimeError):
        worker.recover_startup(right)
    with right:
        assert worker.recover_startup(right) == 0
    with pytest.raises(RuntimeError):
        worker.recover_startup(right)
