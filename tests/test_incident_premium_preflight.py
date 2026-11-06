import asyncio
from dataclasses import asdict

import pytest

from app.database.store import Store
from app.playerok.adapter import APIError
from app.services.paid_promotions import PaidPromotions, intent_key
from app.services.products import Products, ValidationError
from app.services.promotion_settings import PromotionSettings
from app.workers.outbox import Executor, OutboxWorker
from tests.test_paid_promotions import rig as rig_fixture
from tests.test_runtime_promotion_isolation import approve_two

rig = rig_fixture


def restart(r):
    path = r.store.path
    r.store.close()
    r.store = Store(path)
    r.service = PaidPromotions(r.store, r.api)
    r.settings = PromotionSettings(r.store, r.api)
    r.worker = OutboxWorker(r.store, Executor(r.store, r.api, Products(r.store, r.api)))


@pytest.mark.parametrize("failure", ["HTTP_500", "HTTP_429", "cancel", "lease"])
async def test_premium_read_preflight_keeps_original_reservation_and_authority(rig, failure):
    r = rig
    await approve_two(r)
    op = await r.service.queue(7, await r.service.preview(7, "item", "premium"), "original", automatic=True)
    authority = r.store.setting("promotions:paid_authority")
    approval = r.store.setting("promotions:approved:item")
    original = r.api.call

    async def failed(method, **kwargs):
        if method == "get_item":
            if failure == "cancel":
                raise asyncio.CancelledError
            raise APIError(failure, retry_after=60)
        return await original(method, **kwargs)

    r.api.call = failed
    if failure == "lease":
        r.store.claim(operation_id=op)
        r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (op,))
        r.store.recover()
    elif failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await r.worker.once()
    else:
        await r.worker.once()
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (op,)) == [
        {"status": "RETRYABLE_FAILED", "started_at": None}
    ]
    assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "RESERVED"}]
    assert not r.api.charges
    restart(r)
    r.api.call = original
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (op,))
    await r.worker.once()
    assert len(r.api.charges) == 1
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "SUCCEEDED"}]
    assert r.store.setting("promotions:paid_authority") == authority
    assert r.store.setting("promotions:approved:item") == approval
    assert not await r.worker.once()


async def test_budget_consent_is_distinct_from_unchanged_tariff_confirmation(rig):
    r = rig
    await approve_two(r)
    revision, policy, selection = r.settings.get()
    r.settings.save(7, revision, {**asdict(policy), "daily": "200"}, selection)
    fresh = await r.service.preview(7, "item", "premium")
    with pytest.raises(ValidationError, match="Расходы"):
        await r.service.queue(7, fresh, "must-not-spend", automatic=True)
    r.service.arm(7, r.settings.get()[0])
    op = await r.service.queue(7, fresh, "current-consent", automatic=True)
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "SUCCEEDED"}]
    assert r.store.setting("promotions:approved:item")["policy_revision"] == revision
    assert len(r.api.charges) == 1


@pytest.mark.parametrize("requested", ["RETRYABLE_FAILED", "PERMANENT_FAILED"])
@pytest.mark.parametrize("risk", ["intent", "started", "late"])
async def test_preflight_retry_cannot_erase_possible_payment(rig, risk, requested):
    r = rig
    await approve_two(r)
    op = await r.service.queue(7, await r.service.preview(7, "item", "premium"), "risk", automatic=True)
    action = r.store.claim(operation_id=op)
    if risk == "started":
        r.store.mark_started(action)
    elif risk == "intent":
        r.store.set_setting(intent_key(op), {"started_at": "2026-11-06T09:14:00+00:00", "pending": True})
    else:
        r.store.record_late_result(op, "previous-lease", "SUCCEEDED", {"remote_id": "item"})
    r.store.finish(op, action["lease_token"], requested, error="HTTP_500")
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "UNCERTAIN"}]
    restart(r)
    assert not await r.worker.once()
    assert not r.api.charges


@pytest.mark.parametrize("change", ["price", "period", "quote", "stop"])
async def test_restored_network_does_not_approve_changed_money_or_lift_stop(rig, change):
    r = rig
    await approve_two(r)
    op = await r.service.queue(7, await r.service.preview(7, "item", "premium"), "original", automatic=True)
    original = r.api.call

    async def failed(method, **kwargs):
        if method == "get_item":
            raise APIError("HTTP_500")
        return await original(method, **kwargs)

    r.api.call = failed
    await r.worker.once()
    r.api.call = original
    approval = r.store.setting("promotions:approved:item")
    if change == "stop":
        r.settings.stop(7)
    else:
        key, value = {"price": ("price", 11), "period": ("period", 99), "quote": ("id", "different")}[change]
        r.api.quotes[0][key] = value
    restart(r)
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (op,))
    await r.worker.once()
    assert not r.api.charges
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "PERMANENT_FAILED"}]
    assert r.store.setting("promotions:approved:item") == approval


async def test_waiting_read_reserves_budget_but_other_authorized_item_can_finish(rig):
    r = rig
    await approve_two(r)
    first = await r.service.queue(7, await r.service.preview(7, "item", "premium"), "first", automatic=True)
    original = r.api.call

    async def failed(method, **kwargs):
        if method == "get_item" and kwargs["id"] == "item":
            raise APIError("HTTP_500")
        return await original(method, **kwargs)

    r.api.call = failed
    await r.worker.once(operation_id=first)
    r.api.balance = 15
    with pytest.raises(ValidationError, match="баланс"):
        await r.service.queue(
            7, await r.service.preview(7, "other", "premium"), "insufficient", automatic=True
        )
    r.api.balance = 100
    other = await r.service.queue(7, await r.service.preview(7, "other", "premium"), "other", automatic=True)
    await r.worker.once(operation_id=other)
    assert [c["item_id"] for c in r.api.charges] == ["other"]
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (first,)) == [{"status": "RETRYABLE_FAILED"}]


async def test_http500_after_payment_is_unknown_and_never_repeated(rig):
    r = rig
    await approve_two(r)
    op = await r.service.queue(7, await r.service.preview(7, "item", "premium"), "paid", automatic=True)
    charge = r.api.account.increase_item_priority_status

    def lost(**kwargs):
        charge(**kwargs)
        raise APIError("HTTP_500")

    r.api.account.increase_item_priority_status = lost
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "UNCERTAIN"}]
    restart(r)
    assert not await r.worker.once()
    assert len(r.api.charges) == 1
