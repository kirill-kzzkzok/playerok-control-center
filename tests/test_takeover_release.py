"""Adversarial release tests use the real queued PREMIUM launch payload."""

import json
from copy import deepcopy
from dataclasses import asdict

import pytest

from app.database.store import Store
from app.services.paid_promotions import PaidPromotions, intent_key
from app.services.products import Products, ValidationError
from app.services.promotion_settings import PromotionSettings
from app.services.reconciliation import Reconciliation, signature
from app.workers.outbox import Executor, OutboxWorker
from app.workers.promotions import PromotionWorker
from tests.test_gifts import rig  # noqa: F401
from tests.test_listing_launch import configured
from tests.test_listing_launch import launch as launch_fixture


@pytest.fixture
def launch(request):
    return launch_fixture.__wrapped__(request.getfixturevalue("rig"))


async def premium_text(r):
    ident, revision = await configured(
        r,
        mode="text",
        publication="PREMIUM",
        quote_id="premium",
        promotion_policy={
            "per_operation": 50,
            "daily": 100,
            "monthly": 1000,
            "max_operations_per_item": 20,
        },
    )
    operation = await r.launch.queue(7, ident, revision)
    action = r.store.claim(operation_id=operation)
    return ident, operation, action


async def test_real_premium_payload_records_delivery_revision(launch):
    _, operation, action = await premium_text(launch)
    marker = launch.store.setting("launch:prepared:item")
    assert marker["operation"] == operation
    assert action["payload"].get("delivery_revision") == marker["delivery_revision"]


async def test_real_failed_premium_disarms_its_own_policy(launch):
    r = launch
    ident, operation, action = await premium_text(r)
    r.store.finish(operation, action["lease_token"], "PERMANENT_FAILED", error="VALIDATION_FAILED")
    policy = r.store.rows("SELECT payload FROM delivery_policies WHERE item_id='item'")[0]
    assert not json.loads(policy["payload"])["enabled"]
    assert r.store.setting("delivery:item:enabled") is False
    assert r.store.get_draft(ident, 7)["state"] == "DRAFT"


async def test_legacy_marker_fallback_survives_until_never_started_resolution(launch):
    r = launch
    ident, operation, action = await premium_text(r)
    # A persisted operation from 9f5dee1 has the revision only in its marker.
    legacy = {k: v for k, v in action["payload"].items() if k != "delivery_revision"}
    r.store.execute("UPDATE outbox SET payload=? WHERE id=?", (json.dumps(legacy), operation))
    r.store.finish(operation, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
    r.store.set_setting(intent_key(operation), {"operation_id": operation, "pending": True})
    current = r.store.rows("SELECT * FROM outbox WHERE id=?", (operation,))[0]
    await Reconciliation(r.store, r.api).resolve(
        7, operation, signature(current), "STOP_UNCERTAIN", "Оплата не начиналась, остановлено"
    )
    policy = r.store.rows("SELECT payload FROM delivery_policies WHERE item_id='item'")[0]
    assert not json.loads(policy["payload"])["enabled"]
    assert r.store.setting("delivery:item:enabled") is False
    assert r.store.get_draft(ident, 7)["state"] == "DRAFT"


async def test_old_failed_launch_cannot_disable_newer_owner_flag(launch):
    r = launch
    ident, revision = await configured(r, mode="text")
    operation = await r.launch.queue(7, ident, revision)
    action = r.store.claim(operation_id=operation)
    committed = r.store.setting("launch:prepared:item")["delivery_revision"]
    # Ensure this is the real production payload even while F1 is under repair.
    assert action["payload"].get("delivery_revision") == committed
    current_revision, policy = r.launch.automation.get("item")
    await r.launch.automation.save(7, "item", current_revision, asdict(policy), future_confirmed=True)
    r.store.finish(operation, action["lease_token"], "PERMANENT_FAILED", error="VALIDATION_FAILED")
    policy = r.store.rows("SELECT payload FROM delivery_policies WHERE item_id='item'")[0]
    assert json.loads(policy["payload"])["enabled"]
    assert r.store.setting("delivery:item:enabled") is True


@pytest.mark.parametrize("started", [True, False])
async def test_unknown_premium_keeps_spend_blocked_until_never_started_proof(launch, started):
    r = launch
    ident, operation, action = await premium_text(r)
    r.store.finish(operation, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
    intent = {"operation_id": operation, "pending": True}
    if started:
        intent["started_at"] = "2026-11-01T00:00:00+00:00"
    r.store.set_setting(intent_key(operation), intent)
    current = r.store.rows("SELECT * FROM outbox WHERE id=?", (operation,))[0]
    await Reconciliation(r.store, r.api).resolve(
        7, operation, signature(current), "STOP_UNCERTAIN", "Проверено: остановить без повторной отправки"
    )
    assert not r.store.claim(operation_id=operation)
    if started:
        assert r.store.get_draft(ident, 7)["state"] == "QUEUED"
        assert r.store.setting("launch:prepared:item")["operation"] == operation
        assert r.store.rows("SELECT status FROM promotion_expenses")[0]["status"] == "UNCERTAIN"
    else:
        assert r.store.get_draft(ident, 7)["state"] == "DRAFT"


@pytest.mark.parametrize("started", [False, True])
async def test_restart_recovery_keeps_unknown_budget_and_old_rpc_unrepeatable(launch, started):
    r = launch
    ident, operation, action = await premium_text(r)
    if started:
        r.store.set_setting(
            intent_key(operation), {"pending": True, "started_at": "2026-11-01T00:00:00+00:00"}
        )
    r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (operation,))
    path = r.store.path
    r.store.close()
    reopened = Store(path)
    try:
        assert reopened.recover() == 1
        assert not reopened.claim(operation_id=operation)
        assert reopened.get_draft(ident, 7)["state"] == "QUEUED"
        assert reopened.setting("launch:prepared:item")["operation"] == operation
        if started:
            assert reopened.rows("SELECT status FROM promotion_expenses")[0]["status"] == "UNCERTAIN"
            current = reopened.rows("SELECT * FROM outbox WHERE id=?", (operation,))[0]
            await Reconciliation(reopened, r.api).resolve(
                7, operation, signature(current), "STOP_UNCERTAIN", "Проверено после перезапуска: остановить"
            )
        else:
            # No intent or dispatch: recovery retains the reservation and retries
            # PREPARE. Owner STOP must still fence the old launch and old lease.
            assert reopened.rows("SELECT status FROM promotion_expenses")[0]["status"] == "RESERVED"
            assert (
                reopened.rows("SELECT status FROM outbox WHERE id=?", (operation,))[0]["status"]
                == "RETRYABLE_FAILED"
            )
            PromotionSettings(reopened, r.api).stop(7)
            reopened.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (operation,))
            executor = Executor(reopened, r.api, Products(reopened, r.api))
            await OutboxWorker(reopened, executor).once(operation_id=operation)
            assert reopened.rows("SELECT status FROM promotion_expenses")[0]["status"] == "FAILED"
            with pytest.raises(ValidationError):
                await PaidPromotions(reopened, r.api).execute(executor, action)
        assert not reopened.claim(operation_id=operation)
        assert reopened.get_draft(ident, 7)["state"] == ("QUEUED" if started else "DRAFT")
        assert not r.publications and not r.remote.payments
    finally:
        reopened.close()


async def test_release_failure_rolls_back_draft_policy_outbox_and_expense_together(launch):
    r = launch
    ident, operation, action = await premium_text(r)
    # A persistence failure after release has staged policy/draft writes must
    # abort the single transaction, never leave a partly released launch.
    r.store.execute(
        "CREATE TRIGGER fail_launch_release BEFORE DELETE ON settings "
        "WHEN OLD.key='launch:prepared:item' "
        "BEGIN SELECT RAISE(ABORT, 'injected release persistence failure'); END"
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        r.store.finish(operation, action["lease_token"], "PERMANENT_FAILED", error="VALIDATION_FAILED")
    assert r.store.get_draft(ident, 7)["state"] == "QUEUED"
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (operation,))[0]["status"] == "PROCESSING"
    assert r.store.setting("delivery:item:enabled") is True
    assert json.loads(r.store.rows("SELECT payload FROM delivery_policies")[0]["payload"])["enabled"]
    assert r.store.rows("SELECT status FROM promotion_expenses")[0]["status"] == "RESERVED"


async def test_launch_permission_does_not_start_another_lots_dry_run_schedule(launch):
    r = launch
    ident, revision = await configured(
        r,
        mode="manual",
        publication="PREMIUM",
        quote_id="premium",
        promotion=True,
        promotion_policy={
            "enabled": True,
            "per_operation": 50,
            "daily": 100,
            "monthly": 1000,
            "max_operations_per_item": 20,
        },
    )
    other = {**deepcopy(r.api.item), "id": "other", "status": "APPROVED", "priority": "PREMIUM"}
    original = r.api.call

    async def route(method, **kwargs):
        if method == "get_item" and kwargs.get("id") == "other":
            return deepcopy(other)
        return await original(method, **kwargs)

    r.api.call = route
    paid = PaidPromotions(r.store, r.api)
    preview = await paid.preview(7, "other", "premium")
    await paid.approve(7, preview)
    await PromotionSettings(r.store, r.api).target(7, "other", "include")
    assert not r.store.setting("promotions:paid_authority")
    charges = []

    def bump(**kwargs):
        charges.append(kwargs)
        other["status_payment"] = {
            "id": "receipt-other",
            "status": "CONFIRMED",
            "direction": "OUT",
            "operation": "ITEM_PREMIUM_PRIORITY",
            "provider_id": "LOCAL",
            "value": 10,
        }
        return deepcopy(other)

    r.api.account.increase_item_priority_status = bump
    await r.launch.queue(7, ident, revision)
    await PromotionWorker(r.store, r.api).once()
    unexpected = r.store.rows("SELECT id FROM outbox WHERE kind='premium_bump' AND target='other'")
    if unexpected:
        await r.worker.once(operation_id=unexpected[0]["id"])
    assert charges == [], "launch A started B's previously non-spending promotion schedule"
    assert not unexpected


@pytest.mark.parametrize("newer_grant", [False, True])
async def test_failed_launch_releases_only_its_own_scoped_money_authority(launch, newer_grant):
    r = launch
    ident, revision = await configured(
        r,
        mode="manual",
        publication="PREMIUM",
        quote_id="premium",
        promotion=True,
        promotion_policy={
            "enabled": True,
            "per_operation": 50,
            "daily": 100,
            "monthly": 1000,
            "max_operations_per_item": 20,
        },
    )
    operation = await r.launch.queue(7, ident, revision)
    key = "promotions:launch_authority:item"
    own = r.store.setting(key)
    assert own["launch_id"] == ident and own["launch_revision"] == revision
    assert PaidPromotions(r.store, r.api).has_authority("item", own["revision"])
    assert not PaidPromotions(r.store, r.api).has_authority("other", own["revision"])
    if newer_grant:
        # Explicit concurrent owner replacement; the old release has no authority
        # to remove this separate grant even though the shared policy is unchanged.
        r.store.set_setting(key, {**own, "launch_id": "new-owner-approval"})
    action = r.store.claim(operation_id=operation)
    r.store.finish(operation, action["lease_token"], "PERMANENT_FAILED", error="VALIDATION_FAILED")
    if newer_grant:
        assert r.store.setting(key)["launch_id"] == "new-owner-approval"
    else:
        assert not r.store.setting(key)
    assert not r.store.setting("promotions:paid_authority")
