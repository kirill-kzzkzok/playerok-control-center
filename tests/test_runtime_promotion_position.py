"""Category ranking shown by Playerok, not priority tier ranking."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from app.workers.promotions import PromotionWorker
from tests.test_paid_promotions import rig as rig_fixture
from tests.test_runtime_promotion_isolation import approve_two

rig = rig_fixture


async def configure(r):
    revision, policy, selection = r.settings.get()
    r.settings.save(7, revision, {**asdict(policy), "mode": "position", "threshold": 35}, selection)
    await approve_two(r)
    for ident, sequence in (("item", 319), ("other", 320)):
        r.api.items[ident].update(sequence=sequence, priority_position=10)


async def test_category_position_of_both_listings_drives_one_guarded_purchase_each(rig):
    r = rig
    await configure(r)
    worker = PromotionWorker(r.store, r.api)
    await worker.once()
    assert not r.api.charges
    assert len(r.store.rows("SELECT id FROM outbox")) == 2
    assert [
        row["position"]
        for row in r.store.rows("SELECT position FROM promotion_observations ORDER BY item_id")
    ] == [319, 320]
    for _ in range(3):
        await r.worker.once()
    assert sorted(c["item_id"] for c in r.api.charges) == ["item", "other"]
    # Repeated discovery/worker passes cannot duplicate those purchases.
    await worker.once()
    await r.worker.once()
    assert len(r.api.charges) == 2


@pytest.mark.parametrize("position", [None, True, 0, -1, "319"])
async def test_missing_category_position_never_falls_back_to_priority_rank(rig, position):
    r = rig
    await configure(r)
    r.api.items["item"].update(sequence=position, priority_position=319)
    result = await r.settings.evaluate(7, "item", "premium")
    assert result["position"] is None
    assert not result["would_allow"]
    assert any("позиция в категории" in reason.lower() for reason in result["reasons"])
    await r.worker.once()
    assert not r.api.charges


async def test_old_priority_rank_observation_cannot_authorize_new_category_purchase(rig):
    r = rig
    await configure(r)
    r.store.execute(
        "INSERT INTO promotion_observations(item_id,policy_revision,position,checked_at,source) VALUES(?,?,?,?,?)",
        (
            "item",
            r.settings.get()[0],
            319,
            (datetime.now(UTC) - timedelta(seconds=121)).isoformat(),
            "Playerok item.priority_position (priority scope)",
        ),
    )
    result = await r.settings.evaluate(7, "item", "premium")
    assert result["position"] == 319
    assert result["would_allow"]  # The fresh category observation is sufficient on its own.
    assert not r.store.rows("SELECT id FROM outbox")
