"""Negative boundaries for purchased item snapshot delivery; no real API calls."""

from app.gifts.worker import GiftWorker
from app.workers.logical_products import LogicalProductWorker
from tests.test_deal_snapshots import case as case_fixture

case = case_fixture


async def test_full_snapshot_recipient_conflict_cannot_pay_using_older_partial_deal(case):
    r = case
    await LogicalProductWorker(r.store, r.api).once()
    # The fresh full read now proves the required receiving field is absent.
    # An older persisted/partial response must not override this evidence.
    r.full["obtaining_fields"] = []
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert not r.api.remote.payments


async def test_full_snapshot_closed_after_adoption_blocks_partial_paid_replay(case):
    r = case
    await LogicalProductWorker(r.store, r.api).once()
    r.full["status"] = "ROLLED_BACK"
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert not r.api.remote.payments
    assert not await r.worker.once()


async def test_active_catalog_duplicate_is_not_hidden_by_deal_snapshot(case):
    import copy

    from app.services.logical_products import LogicalProducts

    r = case
    for ident in ("active-a", "active-b"):
        r.api.items[ident] = {**copy.deepcopy(r.original), "id": ident, "status": "APPROVED"}
    await LogicalProductWorker(r.store, r.api).once()
    assert LogicalProducts(r.store, r.api).listing("deal-item-copy") is None
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    assert not r.store.rows("SELECT * FROM gift_jobs")
    assert not r.api.remote.payments
