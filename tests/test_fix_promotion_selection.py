from copy import deepcopy
from dataclasses import asdict

import pytest

from app.workers.promotions import PromotionWorker
from tests.test_paid_promotions import rig as rig_fixture

rig = rig_fixture


@pytest.mark.parametrize("excluded", [50, 100, 500])
async def test_inactive_schedule_cannot_consume_selected_slots_or_buy(rig, excluded):
    r = rig
    revision, policy, selection = r.settings.get()
    r.settings.save(7, revision, {**asdict(policy), "mode": "position", "threshold": 35}, selection)
    for ident in ("item", "other"):
        r.api.items[ident].update(status="APPROVED", priority="PREMIUM", sequence=319)
        await r.settings.target(7, ident, "include")
        await r.service.approve(7, await r.service.preview(7, ident, "premium"))
    r.service.arm(7, r.settings.get()[0])
    for i in range(excluded):
        r.store.execute("INSERT INTO promotion_schedule(item_id,due_at) VALUES(?, -1)", (f"excluded-{i:03}",))
    worker = PromotionWorker(r.store, r.api, per_cycle=2)
    await worker.once()
    observed = r.store.rows("SELECT item_id FROM promotion_observations ORDER BY item_id")
    assert {row["item_id"] for row in observed} == {"item", "other"}
    assert not r.store.rows(
        "SELECT item_id FROM promotion_schedule WHERE item_id LIKE 'excluded-%' AND checked_at IS NOT NULL"
    )
    assert not r.store.rows("SELECT * FROM outbox")  # Only first sequence observation.
    # Switching modes neither drops excluded rows nor invalidates exact approvals.
    revision, policy, _ = r.settings.get()
    approval = deepcopy(r.store.setting("promotions:approved:item"))
    r.settings.save(7, revision, asdict(policy), "all")
    r.api.items["excluded-000"] = {**deepcopy(r.api.items["item"]), "id": "excluded-000"}
    assert worker.claim(r.settings.get()[0]) is not None
    revision, policy, _ = r.settings.get()
    r.settings.save(7, revision, asdict(policy), "selected")
    r.store.execute("UPDATE promotion_schedule SET lease_until=NULL")
    assert worker.claim(r.settings.get()[0])[0] in {"item", "other"}
    assert r.store.setting("promotions:approved:item") == approval
