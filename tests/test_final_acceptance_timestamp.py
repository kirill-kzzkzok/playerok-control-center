"""Existing/imported drafts retain strict future-only cutoff.

The takeover RED counterexample is preserved in its ignored artifact. A fresh
create receipt is needed to solve it; generic re-arm is deliberately not floored.
"""

from datetime import datetime

import pytest

from app.services.orders import Orders
from app.workers.enrollment import EnrollmentWorker
from tests.test_gifts import rig as rig_fixture
from tests.test_listing_launch import configured
from tests.test_listing_launch import launch as launch_fixture


@pytest.fixture(name="rig")
def _rig(tmp_path):
    yield from rig_fixture.__wrapped__(tmp_path)


@pytest.fixture(name="launch")
def _launch(rig):
    return launch_fixture.__wrapped__(rig)


@pytest.mark.parametrize("mode", ["gifts", "text"])
async def test_unproven_draft_coarse_timestamp_cannot_enable_retroactive_delivery(launch, mode):
    r = launch
    ident, revision = await configured(r, mode=mode)
    await r.launch.queue(7, ident, revision)
    await r.worker.once()
    if mode == "gifts":
        armed = r.service.listing("item")["armed_at"]
    else:
        armed = r.store.rows("SELECT updated_at FROM delivery_policies WHERE item_id='item'")[0]["updated_at"]
    r.api.orders["immediate"]["created_at"] = datetime.fromisoformat(armed).replace(microsecond=0).isoformat()
    Orders(r.store).ingest(r.api.orders["immediate"])
    if mode == "gifts":
        assert r.service.discover() == 0
    else:
        await EnrollmentWorker(r.store, r.api).once()
        assert not r.store.rows("SELECT 1 FROM delivery_chains WHERE order_id='immediate'")
        assert (
            r.store.rows("SELECT status FROM auto_enrollment WHERE order_id='immediate'")[0]["status"]
            == "SKIPPED"
        )
    assert not r.remote.payments
