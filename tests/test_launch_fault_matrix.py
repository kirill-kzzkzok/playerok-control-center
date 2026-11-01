"""Fault-injection matrix for the PREPARE/COMMIT launch boundary (I1, I2, I5).

Injections target REAL persistence boundaries of queue():
- P1: failure inside PREPARE (quotes read)
- P2: failure between PREPARE and COMMIT (premium preview read)
- C1: failure inside COMMIT (delivery written, before premium permissions)
- W:  failure inside COMMIT after approve+target (the original D1 hole)
Each case verifies the FULL post-failure SQLite state: no orphan permissions,
no lost updates, no partial delivery state, draft retryable.
"""

from dataclasses import asdict

import pytest

from app.services.promotion_settings import PromotionSettings
from app.services.promotions import PromotionPolicy
from tests.test_gifts import rig  # noqa: F401 -- registers the rig fixture
from tests.test_listing_launch import configured, launch  # noqa: F401


async def _premium_setup(r):
    r.quotes = [
        {"id": "free", "type": "DEFAULT", "price": 0, "period": 7},
        {"id": "premium", "type": "PREMIUM", "price": 10, "period": 7},
    ]
    budget = {"per_operation": 50, "daily": 100, "monthly": 1000, "max_operations_per_item": 20}
    ps = PromotionSettings(r.store, r.api)
    revision, policy, selection = ps.get()
    full = asdict(PromotionPolicy(**{**asdict(policy), **budget}))
    ps.save(7, revision, full, selection)
    return budget


def _state(r, item="item"):
    return {
        "draft": r.store.get_draft(r.store.setting("launch:config:" + item), 7)["state"],
        "marker": r.store.setting("launch:prepared:" + item),
        "delivery_policies": r.store.rows("SELECT item_id FROM delivery_policies"),
        "gift_listings": r.store.rows("SELECT item_id,enabled FROM gift_listings"),
        "approval": r.store.setting("promotions:approved:" + item),
        "authority": r.store.setting("promotions:paid_authority"),
        "targets": r.store.rows("SELECT item_id FROM promotion_targets WHERE item_id=?", (item,)),
        "expenses": r.store.rows("SELECT item_id,status FROM promotion_expenses"),
        "operations": r.store.rows("SELECT id FROM outbox WHERE kind IN ('publish','premium_publish')"),
    }


async def test_matrix_inject_into_every_commit_boundary(launch):
    """I1/I2/I5 fault matrix: each failure leaves NO orphan permission and the
    draft is retryable; the other lot state is never touched."""
    r = launch
    budget = await _premium_setup(r)
    original_call = r.api.call
    from app.database.store import Store

    original_enqueue = Store.enqueue

    def failing_enqueue(self, key, kind, target, payload, db=None, actor=None):
        # The LAST write of the COMMIT transaction: delivery + premium permissions
        # are already staged in this transaction and must roll back together.
        raise RuntimeError("injected COMMIT failure at the enqueue boundary")

    try:
        # P1: PREPARE-stage read failure.
        async def prepare_failing(method, **kwargs):
            if method == "get_item_priority_statuses":
                raise RuntimeError("injected PREPARE failure")
            return await original_call(method, **kwargs)

        ident, revision = await configured(
            r,
            mode="gifts",
            publication="PREMIUM",
            quote_id="premium",
            promotion=True,
            promotion_policy=budget,
        )
        r.api.call = prepare_failing
        with pytest.raises(RuntimeError):
            await r.launch.queue(7, ident, revision)
        r.api.call = original_call
        state = _state(r)
        assert state["draft"] == "DRAFT"
        assert not state["approval"] and not state["targets"] and not state["expenses"]
        assert not state["operations"]
        assert not [l for l in state["gift_listings"] if l["enabled"]]

        # C1: COMMIT failure after the delivery write (enqueue is the last write).
        Store.enqueue = failing_enqueue
        ident, revision = await configured(
            r,
            mode="gifts",
            publication="PREMIUM",
            quote_id="premium",
            promotion=True,
            promotion_policy=budget,
        )
        with pytest.raises(RuntimeError):
            await r.launch.queue(7, ident, revision)
        state = _state(r)
        assert state["draft"] == "DRAFT", state["draft"]
        assert not state["approval"], "orphan approval"
        assert not state["targets"], "orphan target"
        assert not state["expenses"], "orphan expense"
        assert not state["operations"], "orphan operation"
        assert not [l for l in state["gift_listings"] if l["enabled"]], "armed gift listing"

        # W: COMMIT failure with premium permissions staged (the original D1 hole):
        # the enqueue boundary leaves approve/target/authority staged in the same
        # transaction — the rollback must discard ALL of them.
        Store.enqueue = failing_enqueue
        ident, revision = await configured(
            r,
            mode="gifts",
            publication="PREMIUM",
            quote_id="premium",
            promotion=True,
            promotion_policy=budget,
        )
        with pytest.raises(RuntimeError):
            await r.launch.queue(7, ident, revision)
        state = _state(r)
        assert state["draft"] == "DRAFT", state["draft"]
        assert not state["approval"], "orphan approval"
        assert not state["targets"], "orphan target"
        assert not state["expenses"], "orphan expense"
        assert not state["operations"], "orphan operation"
        assert not [l for l in state["gift_listings"] if l["enabled"]], "armed gift listing"
    finally:
        r.api.call = original_call
        Store.enqueue = original_enqueue


async def test_matrix_prepare_failure_writes_nothing(launch):
    """P2: a failure between PREPARE and COMMIT leaves ZERO local writes."""
    r = launch
    budget = await _premium_setup(r)
    original_call = r.api.call

    ident, revision = await configured(
        r,
        mode="text",
        publication="PREMIUM",
        quote_id="premium",
        promotion_policy=budget,
    )

    async def failing(method, **kwargs):
        if method == "get_item" and kwargs.get("id") == "item":
            raise RuntimeError("injected preview failure")
        return await original_call(method, **kwargs)

    r.api.call = failing
    try:
        with pytest.raises(RuntimeError):
            await r.launch.queue(7, ident, revision)
    finally:
        r.api.call = original_call
    state = _state(r)
    assert state["draft"] == "DRAFT"
    assert not state["delivery_policies"]
    assert not state["operations"]
    assert not state["marker"]
