"""Production launch, delivery and Dispatcher; no live HTTP or paid Telegram RPCs."""

import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, now
from app.gifts.worker import GiftWorker
from app.playerok.adapter import APIError
from app.services.listing_launch import ListingLaunch
from app.services.orders import Orders
from app.services.products import ValidationError
from app.telegram.storage import SQLiteStorage
from tests.test_gifts import rig  # noqa: F401 -- shared temporary SQLite and real TL test transport
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


@pytest.fixture
def launch(rig):
    r = rig
    r.api.item.update(status="DRAFT", price=100, raw_price=100)
    r.store.set_setting("sync:health", {"status": "OK"})
    r.store.set_setting("gifts:stopped", False)  # explicit owner permission in isolated fixture
    original = r.api.call
    r.quotes = [
        {"id": "free", "type": "DEFAULT", "price": 0, "period": 7},
        {"id": "premium", "type": "PREMIUM", "price": 10, "period": 7},
    ]
    r.publications = []
    r.failure = None

    async def call(method, **kwargs):
        if method == "get_item_priority_statuses":
            return deepcopy(r.quotes)
        if method == "get":
            return {"id": "shop", "profile": {"balance": {"available": 100}}}
        if method == "get_my_items":
            return {"items": [deepcopy(r.api.item)], "page_info": {}}
        return await original(method, **kwargs)

    original_permit = r.api.account.permit

    @contextmanager
    def permit(operation):
        if operation == "publishItem" and r.failure == "before":
            raise ValidationError("Read-only publication preflight refused")
        with original_permit(operation):
            yield

    r.api.account.permit = permit

    def publish(**kwargs):
        binding = r.service.listing("item")
        policy = r.store.rows("SELECT * FROM delivery_policies WHERE item_id='item'")
        r.publications.append({"binding": deepcopy(binding), "policy": deepcopy(policy)})
        r.api.item["status"] = "APPROVED"
        if r.premium:
            r.api.item["priority"] = "PREMIUM"
            r.api.item["status_payment"] = {
                "id": "receipt-launch",
                "status": "CONFIRMED",
                "direction": "OUT",
                "operation": "ITEM_PREMIUM_PRIORITY",
                "provider_id": "LOCAL",
                "value": 10,
            }
        Orders(r.store).ingest(r.api.add_order("immediate"))
        if r.failure == "lost_ack":
            raise APIError("NETWORK_ERROR", uncertain=True)
        return deepcopy(r.api.item)

    r.premium = False
    r.api.call = call
    r.api.account.publish_item = publish
    r.launch = ListingLaunch(r.store, r.api, r.service)
    return r


async def configured(r, mode="gifts", **options):
    r.premium = options.get("publication") == "PREMIUM"
    if "promotion_policy" in options:
        # The budget lives in the global policy (as the real UI saves it), the launch
        # draft only carries the confirmed full copy.
        from dataclasses import asdict

        from app.services.promotion_settings import PromotionSettings
        from app.services.promotions import PromotionPolicy

        revision, policy, selection = PromotionSettings(r.store, r.api).get()
        full = asdict(PromotionPolicy(**{**asdict(policy), **options["promotion_policy"]}))
        PromotionSettings(r.store, r.api).save(7, revision, full, selection)
        options = {**options, "promotion_policy": full}
    ident = await r.launch.open(
        7,
        "item",
        {"delivery": mode, "delivery_data": "secret-one\nsecret-two" if mode == "stock" else "secret-text"},
    )
    draft = r.launch.load(7, ident)
    value = draft["payload"]
    value.update(options)
    value["manual_delivery_confirmed"] = mode == "manual"
    value["ready"] = await r.launch.check(7, value)
    r.store.update_draft(ident, 7, draft["revision"], value)
    return ident, r.launch.load(7, ident)["revision"]


async def test_gifts_armed_before_publication_and_first_immediate_sale_is_enrolled(launch):
    r = launch
    ident, revision = await configured(r, auto_complete=True, after="Thanks")
    operation = await r.launch.queue(7, ident, revision)
    assert r.api.item["status"] == "DRAFT"
    armed = r.service.listing("item")
    assert armed["enabled"] and armed["auto_fulfill"]
    assert not r.remote.payments
    assert await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (operation,))[0]["status"] == "SUCCEEDED"
    assert r.publications[0]["binding"]["armed_at"] == armed["armed_at"]
    assert r.service.discover() == 1
    assert await r.worker.once()
    assert len(r.remote.payments) == 1
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    await r.worker.once()
    assert r.api.orders["immediate"]["status"] == "SENT"
    assert r.api.sent[0][1] == "Thanks"


async def test_premium_launch_reserves_pending_income_without_sending_payment(launch):
    r = launch
    original = r.api.call

    async def call(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get":
            result["profile"]["balance"].update(available=5, pending_income=53)
        return result

    r.api.call = call
    ident, revision = await configured(r, publication="PREMIUM", quote_id="premium", manual_premium=True)
    assert r.launch.load(7, ident)["payload"]["ready"]["balance"] == "53.00"
    operation = await r.launch.queue(7, ident, revision)
    assert r.store.rows("SELECT kind,status FROM outbox WHERE id=?", (operation,)) == [
        {"kind": "premium_publish", "status": "PENDING"}
    ]
    assert r.store.rows("SELECT amount,status FROM promotion_expenses") == [
        {"amount": "10.00", "status": "RESERVED"}
    ]
    assert not r.publications and not r.remote.payments


@pytest.mark.parametrize("mode", ["text", "stock"])
async def test_prepared_text_and_stock_are_enabled_before_publication(launch, mode):
    r = launch
    ident, revision = await configured(r, mode)
    op = await r.launch.queue(7, ident, revision)
    assert not r.service.listing("item")
    assert await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "SUCCEEDED"
    saved = r.publications[0]["policy"][0]
    assert json.loads(saved["payload"])["auto_enroll"] is True
    assert json.loads(saved["payload"])["enabled"] is True
    assert saved["updated_at"] <= now()
    if mode == "stock":
        assert len(r.store.rows("SELECT * FROM inventory WHERE item_id='item' AND status='AVAILABLE'")) == 2


async def test_restart_between_preparation_and_publish_no_second_queue_or_late_arm(launch):
    r = launch
    ident, revision = await configured(r)
    await r.launch.queue(7, ident, revision)
    armed = r.service.listing("item")["armed_at"]
    resumed = ListingLaunch(r.store, r.api, r.service)
    with pytest.raises(Conflict):
        await resumed.queue(7, ident, revision)
    await r.worker.once()
    assert len(r.publications) == 1 and r.service.listing("item")["armed_at"] == armed


@pytest.mark.parametrize("change", ["price", "account", "description", "gift_price", "config"])
async def test_changed_authority_or_price_invalidates_confirmation(launch, change):
    r = launch
    ident, revision = await configured(r)
    if change == "price":
        r.api.item["price"] += 1
    elif change == "account":
        r.api.account.id = "other"
    elif change == "description":
        r.api.item["description"] += "\nchanged"
    elif change == "gift_price":
        r.remote.gift.stars += 1
    else:
        draft = r.launch.load(7, ident)
        draft["payload"]["after"] = "changed"
        r.store.update_draft(ident, 7, revision, draft["payload"])
    with pytest.raises((ValueError, Conflict)):
        await r.launch.queue(7, ident, revision)
    assert not r.publications and not r.remote.payments


@pytest.mark.parametrize("failure", ["before", "lost_ack"])
async def test_publication_failure_and_unknown_never_republish(launch, failure):
    r = launch
    ident, revision = await configured(r)
    r.failure = failure
    op = await r.launch.queue(7, ident, revision)
    await r.worker.once()
    status = r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"]
    assert status == ("UNCERTAIN" if failure == "lost_ack" else "PERMANENT_FAILED")
    with pytest.raises(Conflict):
        await r.launch.queue(7, ident, revision)
    await r.worker.once()
    assert len(r.publications) == (1 if failure == "lost_ack" else 0)
    if failure == "lost_ack":
        assert r.api.item["status"] == "APPROVED" and r.service.listing("item")["enabled"]
        assert r.service.discover() == 1
    else:
        assert r.api.item["status"] == "DRAFT"


async def test_stop_after_queue_blocks_publication(launch):
    r = launch
    ident, revision = await configured(r)
    await r.launch.queue(7, ident, revision)
    r.service.stop(7)
    await r.worker.once()
    assert not r.publications and not r.remote.payments


async def test_failed_preparation_releases_marker_but_cannot_bypass_launch(launch):
    r = launch
    # A running text auto-delivery on the same item makes manual-mode queue() fail
    # exactly at the stop-check, after launch:prepared was already recorded.
    ident, revision = await configured(r, mode="manual")
    # An enabled delivery policy on the same item makes queue() fail at the
    # stop-check, after launch:prepared was already recorded.
    r.store.execute(
        'INSERT INTO delivery_policies VALUES(\'item\',1,7,\'{"enabled": true, "mode": "text",'
        ' "text": "x", "greeting": "", "after": "", "mark_sent": false, "auto_enroll": true}\','
        " '2026-10-31T00:00:00+00:00')"
    )
    with pytest.raises(Conflict):
        await r.launch.queue(7, ident, revision)
    assert r.store.get_draft(ident, 7)["state"] == "DRAFT"
    assert not r.store.setting("launch:prepared:item")
    op = r.store.enqueue(
        "plain:publish2", "publish", "item", {"priority_id": "free", "price": 100, "raw_price": 100}
    )
    await r.worker.once()
    row = r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]
    assert row["status"] == "PERMANENT_FAILED", row
    assert not r.publications and r.api.item["status"] == "DRAFT"
    r.launch.automation.stop_all(7)
    draft = r.launch.load(7, ident)
    value = draft["payload"]
    value["ready"] = await r.launch.check(7, value)
    r.store.update_draft(ident, 7, draft["revision"], value)
    await r.launch.queue(7, ident, r.launch.load(7, ident)["revision"])
    await r.worker.once()
    assert r.publications and r.api.item["status"] == "APPROVED"


async def test_permanent_failure_releases_the_item_for_a_new_launch_or_publish(launch):
    r = launch
    ident, revision = await configured(r)
    r.failure = "before"
    op = await r.launch.queue(7, ident, revision)
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "PERMANENT_FAILED"
    assert r.api.item["status"] == "DRAFT"
    # The failed launch must not leave live auto-delivery behind (gift listing armed
    # before publication has to be disarmed together with the draft release).
    assert not (r.service.listing("item") or {}).get("enabled")
    # A retry queues a NEW operation (revision was bumped by the release).
    draft = r.store.get_draft(ident, 7)
    r.failure = None
    value = draft["payload"]
    value["ready"] = await r.launch.check(7, value)
    r.store.update_draft(ident, 7, draft["revision"], value)
    op2 = await r.launch.queue(7, ident, r.store.get_draft(ident, 7)["revision"])
    assert op2 != op
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op2,))[0]["status"] == "SUCCEEDED"
    assert r.publications and r.api.item["status"] == "APPROVED"


async def test_local_stop_before_publication_is_permanent_not_uncertain(launch):
    r = launch
    ident, revision = await configured(r)
    op = await r.launch.queue(7, ident, revision)
    r.service.stop(7)  # owner stops gift automation before the publish executes
    await r.worker.once()
    row = r.store.rows("SELECT status,error_code FROM outbox WHERE id=?", (op,))[0]
    assert row["status"] == "PERMANENT_FAILED", row
    assert not r.publications and r.api.item["status"] == "DRAFT"


async def test_failed_preparation_disarms_gift_listing_it_just_armed(launch):
    r = launch
    # Scenario A: gifts are bound+armed inside queue(), then the publication step
    # fails BEFORE any outbox operation exists (PREMIUM balance drops mid-flight).
    r.api.item["status"] = "DRAFT"
    r.quotes = [
        {"id": "free", "type": "DEFAULT", "price": 0, "period": 7},
        {"id": "premium", "type": "PREMIUM", "price": 10, "period": 7},
    ]

    async def failing_call(method, **kwargs):
        if method == "get":
            return {"id": "shop", "profile": {"balance": {"available": 0}}}
        return await r.api.call(method, **kwargs)

    original_call = r.api.call
    budget = {"per_operation": 50, "daily": 100, "monthly": 1000, "max_operations_per_item": 20}
    ident, revision = await configured(
        r, mode="gifts", publication="PREMIUM", quote_id="premium", promotion_policy=budget
    )

    # Drop the balance AFTER readiness was confirmed: queue() re-checks it inside
    # PaidPromotions.queue and must fail after the gift listing was already armed.
    async def depleted(method, **kwargs):
        if method == "get":
            return {"id": "shop", "profile": {"balance": {"available": 0}}}
        return await original_call(method, **kwargs)

    r.api.call = depleted
    with pytest.raises((ValidationError, Conflict)):
        await r.launch.queue(7, ident, revision)
    r.api.call = original_call
    # Partial preparation is fully rolled back: no armed gift spending, no marker,
    # draft reusable, and a plain publish of the same item still works.
    listing = r.service.listing("item")
    assert not listing or not listing["enabled"]
    assert not r.store.setting("launch:prepared:item")
    assert r.store.get_draft(ident, 7)["state"] == "DRAFT"
    assert not r.remote.payments and r.api.item["status"] == "DRAFT"


async def test_failed_premium_promotion_launch_revokes_only_its_own_permissions(launch):
    r = launch
    budget = {"per_operation": 50, "daily": 100, "monthly": 1000, "max_operations_per_item": 20}
    # Open lot A's draft FIRST (it snapshots the default policy), then lot B's
    # configured() saves the global budget, then refresh A's draft payload copy.
    r.api.item2 = {**r.api.item, "id": "item2", "status": "DRAFT"}
    original_call = r.api.call

    async def call_two(method, **kwargs):
        if method == "get_item" and kwargs.get("id") == "item2":
            return deepcopy(r.api.item2)
        if method == "get_my_items":
            return {"items": [deepcopy(r.api.item2)], "page_info": {}}
        return await original_call(method, **kwargs)

    r.api.call = call_two
    ident_a = await r.launch.open(7, "item2", {"delivery": "text", "delivery_data": "a-secret"})
    r.api.call = original_call

    # Lot B: an independent PREMIUM launch with paid promotion succeeds; its
    # permissions must survive the failed attempt on lot A.
    ident_b, revision_b = await configured(
        r, mode="text", publication="PREMIUM", quote_id="premium", promotion=True, promotion_policy=budget
    )
    op_b = await r.launch.queue(7, ident_b, revision_b)
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op_b,))[0]["status"] == "SUCCEEDED"
    approval_b = r.store.setting("promotions:approved:item")
    target_b = r.store.rows("SELECT disposition FROM promotion_targets WHERE item_id='item'")
    assert approval_b and target_b == [{"disposition": "include"}]

    # Lot A's launch fails at the paid.queue stage — AFTER approve/target/arm
    # created fresh permissions for A.
    r.api.call = call_two
    draft_a = r.launch.load(7, ident_a)
    value_a = draft_a["payload"]
    # Draft A was opened before the budget existed: sync its policy copy with the
    # live global policy (as the launch UI does after saving budget/schedule).
    from app.services.promotion_settings import PromotionSettings as _PS

    _, live_policy, _ = _PS(r.store, r.api).get()
    value_a["promotion_policy"] = asdict(live_policy)
    value_a.update(publication="PREMIUM", quote_id="premium", promotion=True)
    value_a["ready"] = await r.launch.check(7, value_a)
    r.store.update_draft(ident_a, 7, draft_a["revision"], value_a)
    revision_a = r.launch.load(7, ident_a)["revision"]

    from app.database.store import Store as _Store

    original_enqueue = _Store.enqueue

    def failing_enqueue(self, key, kind, target, payload, db=None, actor=None):
        # The LAST write of the COMMIT transaction: premium permissions are already
        # staged in this transaction and must roll back together with it.
        raise RuntimeError("injected failure at the enqueue boundary")

    _Store.enqueue = failing_enqueue
    try:
        with pytest.raises(RuntimeError):
            await r.launch.queue(7, ident_a, revision_a)
    finally:
        _Store.enqueue = original_enqueue
    r.api.call = original_call

    # Lot A's own fresh permissions are revoked; lot B's survive untouched.
    assert not r.store.setting("promotions:approved:item2")
    assert r.store.setting("promotions:approved:item") == approval_b
    assert r.store.rows("SELECT disposition FROM promotion_targets WHERE item_id='item'") == [
        {"disposition": "include"}
    ]
    assert not r.store.rows("SELECT 1 FROM promotion_targets WHERE item_id='item2'")
    # Lot A's draft is released and retryable.
    assert r.store.get_draft(ident_a, 7)["state"] == "DRAFT"


async def test_launch_policy_change_does_not_touch_other_lots_approvals(launch):
    r = launch

    budget = {"per_operation": 50, "daily": 100, "monthly": 1000, "max_operations_per_item": 20}
    # Lot B: an already-approved PREMIUM tariff with its own promotion settings.
    ident_b, revision_b = await configured(
        r, mode="text", publication="PREMIUM", quote_id="premium", promotion=True, promotion_policy=budget
    )
    op_b = await r.launch.queue(7, ident_b, revision_b)
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op_b,))[0]["status"] == "SUCCEEDED"
    approval_b = r.store.setting("promotions:approved:item")
    assert approval_b["item"]["id"] == "item"

    # Lot A launch with a HIGHER budget: its save must not invalidate B's approval.
    higher = {"per_operation": 80, "daily": 200, "monthly": 2000, "max_operations_per_item": 30}
    from dataclasses import asdict

    from app.services.promotion_settings import PromotionSettings
    from app.services.promotions import PromotionPolicy

    ps = PromotionSettings(r.store, r.api)
    revision, policy, selection = ps.get()
    ps.save(7, revision, asdict(PromotionPolicy(**{**asdict(policy), **higher})), selection)
    approval_after = r.store.setting("promotions:approved:item")
    # The stored approval for B still exists with its confirmed tariff fields.
    assert approval_after["item"]["id"] == "item"
    assert approval_after["quote_id"] == approval_b["quote_id"]
    assert approval_after["amount"] == approval_b["amount"]


async def test_premium_launch_with_prepared_auto_delivery(launch):
    r = launch
    budget = {"per_operation": 50, "daily": 100, "monthly": 1000, "max_operations_per_item": 20}
    ident, revision = await configured(
        r, mode="text", publication="PREMIUM", quote_id="premium", promotion_policy=budget
    )
    op = await r.launch.queue(7, ident, revision)
    assert r.store.rows("SELECT kind FROM outbox WHERE id=?", (op,))[0]["kind"] == "premium_publish"
    assert not r.publications and not r.remote.payments
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "SUCCEEDED"
    assert r.publications and r.api.item["status"] == "APPROVED"
    saved = r.publications[0]["policy"][0]
    assert json.loads(saved["payload"])["enabled"] is True
    assert r.service.discover() == 0  # nothing to enroll before a sale
    # No bump was dispatched: the only expense record is the launch reservation itself.
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind='premium_bump'")


async def test_premium_launch_with_paid_promotion_prepared_before_publication(launch):
    r = launch
    budget = {"per_operation": 50, "daily": 100, "monthly": 1000, "max_operations_per_item": 20}
    ident, revision = await configured(
        r, mode="text", publication="PREMIUM", quote_id="premium", promotion=True, promotion_policy=budget
    )
    op = await r.launch.queue(7, ident, revision)
    assert not r.publications and not r.remote.payments
    prepared = r.store.setting("launch:prepared:item")
    assert prepared and prepared["operation"] == op
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "SUCCEEDED"
    assert r.publications and r.api.item["status"] == "APPROVED"
    # Promotion policy armed before publication; no bump was executed yet (freshly
    # published item, zero schedule ticks).
    assert r.store.setting("promotions:approved:item", {}).get("item", {}).get("id") == "item"
    assert not r.remote.payments


async def test_empty_stock_blocks_launch_before_any_operation(launch):
    r = launch
    ident = await r.launch.open(7, "item", {"delivery": "stock", "delivery_data": ""})
    draft = r.launch.load(7, ident)
    value = draft["payload"]
    with pytest.raises(ValidationError):
        await r.launch.check(7, value)
    assert not r.store.rows("SELECT 1 FROM outbox WHERE operation_key LIKE 'launch:%'")
    assert not r.publications and r.api.item["status"] == "DRAFT"


async def test_pre_existing_policy_survives_failed_manual_launch(launch):
    r = launch
    # Scenario A: an owner-configured enabled text policy exists; a MANUAL launch
    # conflicts with it — the failed launch must not touch the pre-existing policy.
    r.store.execute(
        'INSERT INTO delivery_policies VALUES(\'item\',1,7,\'{"enabled": true, "mode": "text",'
        ' "text": "x", "greeting": "", "after": "", "mark_sent": false, "auto_enroll": true}\','
        " '2026-10-31T00:00:00+00:00')"
    )
    r.store.set_setting("delivery:item:enabled", True)
    ident = await r.launch.open(7, "item", {"delivery": "manual"})
    draft = r.launch.load(7, ident)
    value = draft["payload"]
    value["ready"] = await r.launch.check(7, value)
    r.store.update_draft(ident, 7, draft["revision"], value)
    revision = r.launch.load(7, ident)["revision"]
    with pytest.raises(Conflict):
        await r.launch.queue(7, ident, revision)
    policy = r.store.rows("SELECT payload FROM delivery_policies WHERE item_id='item'")[0]
    assert json.loads(policy["payload"])["enabled"] is True
    assert r.store.setting("delivery:item:enabled") is True
    # Scenario E: another lot's automation is never touched by this failure.
    r.store.execute(
        'INSERT INTO delivery_policies VALUES(\'other\',1,7,\'{"enabled": true, "mode": "text",'
        ' "text": "y", "greeting": "", "after": "", "mark_sent": false, "auto_enroll": true}\','
        " '2026-10-31T00:00:00+00:00')"
    )
    other = r.store.rows("SELECT payload FROM delivery_policies WHERE item_id='other'")[0]
    assert json.loads(other["payload"])["enabled"] is True


async def test_launch_created_policy_is_gone_after_failed_preparation(launch):
    r = launch
    # Scenario B (text variant): the launch itself creates+enables the policy, then
    # the preparation fails BEFORE enqueue — the policy the launch created is removed.
    original_call = r.api.call

    async def failing(method, **kwargs):
        if method == "get_item_priority_statuses":
            raise RuntimeError("injected after policy save")
        return await original_call(method, **kwargs)

    # text mode: queue() saves a policy; break the quotes/preview stage after it.
    ident2, revision2 = await configured(r, mode="text")
    assert not r.store.rows("SELECT 1 FROM delivery_policies WHERE item_id='item'")
    r.api.call = failing
    try:
        with pytest.raises(RuntimeError):
            await r.launch.queue(7, ident2, revision2)
    finally:
        r.api.call = original_call
    # The policy was created by THIS launch (none existed before): it is removed.
    assert not r.store.rows("SELECT 1 FROM delivery_policies WHERE item_id='item'")
    assert r.store.get_draft(ident2, 7)["state"] == "DRAFT"


async def test_second_attempt_failure_releases_even_after_dead_prior_operation(launch):
    r = launch
    # Attempt 1: launch fails terminally; release bumps the draft revision.
    ident, revision = await configured(r)
    r.failure = "before"
    op1 = await r.launch.queue(7, ident, revision)
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op1,))[0]["status"] == "PERMANENT_FAILED"
    draft = r.store.get_draft(ident, 7)
    assert draft["state"] == "DRAFT" and draft["revision"] > revision
    # Attempt 2 on the new revision: preparation fails BEFORE creating the operation
    # (manual mode + an enabled delivery policy on the item).
    r.store.execute(
        'INSERT INTO delivery_policies VALUES(\'item\',1,7,\'{"enabled": true, "mode": "text",'
        ' "text": "x", "greeting": "", "after": "", "mark_sent": false, "auto_enroll": true}\','
        " '2026-10-31T00:00:00+00:00')"
    )
    value = draft["payload"]
    value["mode"] = "manual"
    r.failure = None
    value["ready"] = await r.launch.check(7, value)
    r.store.update_draft(ident, 7, draft["revision"], value)
    revision2 = r.store.get_draft(ident, 7)["revision"]
    with pytest.raises(Conflict):
        await r.launch.queue(7, ident, revision2)
    # The stale dead operation of revision 1 must NOT keep attempt 2 stuck:
    # the draft returns to DRAFT, the marker is gone. The delivery policy was
    # PRE-EXISTING owner configuration (not created by this launch), so the
    # rollback must leave it exactly as it was — still enabled.
    after = r.store.get_draft(ident, 7)
    assert after["state"] == "DRAFT", after
    assert not r.store.setting("launch:prepared:item")
    policy = r.store.rows("SELECT payload FROM delivery_policies WHERE item_id='item'")[0]
    assert json.loads(policy["payload"])["enabled"] is True


async def test_queued_draft_screen_links_the_real_operation(launch):
    r = launch
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    try:
        ident, revision = await configured(r)
        op = await r.launch.queue(7, ident, revision)
        await ui.click("launch:open:" + ident)
        markup = session.calls[-1].reply_markup
        result_buttons = [
            b.callback_data
            for row in markup.inline_keyboard
            for b in row
            if "result:show:" in (b.callback_data or "")
        ]
        assert result_buttons == ["result:show:" + op], result_buttons
        # Prefix isolation: another draft id never matches this launch's operations.
        from app.services.listing_launch import launch_prefix

        assert not r.store.rows(
            "SELECT 1 FROM outbox WHERE operation_key LIKE ?", (launch_prefix(ident + "ff"),)
        )
    finally:
        await bot.session.close()


async def test_dispatcher_premium_budget_schedule_saved_globally_before_launch(launch):
    """RED for defect 1: fresh install (zero global limits) — the launch wizard must
    actually save the PREMIUM budget/schedule to the global policy, not only to the
    draft, so check() does not reject values the owner just entered."""
    r = launch
    r.quotes = [
        {"id": "free", "type": "DEFAULT", "price": 0, "period": 7},
        {"id": "premium", "type": "PREMIUM", "price": 10, "period": 7},
    ]
    r.premium = True
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("launch:item:item")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:mode:text")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:edit:data")
        ui.dp = dispatcher(r.store, r.api)
        await ui.message("Secret delivery text")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:quotes")
        nonce = (await state.get_data())["launch"]["nonce"]
        # Pick the PREMIUM quote.
        premium_index = next(i for i, q in enumerate(r.quotes) if q["type"] == "PREMIUM")
        await ui.click(f"ln:{nonce}:quote:{premium_index}")
        nonce = (await state.get_data())["launch"]["nonce"]
        draft_id = (await state.get_data())["launch"]["id"]
        await ui.click(f"ln:{nonce}:edit:budget")
        ui.dp = dispatcher(r.store, r.api)
        for field, value in (
            ("per_operation", "50"),
            ("daily", "100"),
            ("monthly", "1000"),
            ("max_operations_per_item", "20"),
        ):
            nonce = (await state.get_data())["promotion"]["nonce"]
            await ui.click(f"pp:{nonce}:edit:{field}")
            await ui.message(value)
        nonce = (await state.get_data())["promotion"]["nonce"]
        await ui.click(f"pp:{nonce}:save")
        await ui.click("launch:policy:" + draft_id)
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:edit:schedule")
        for field, value in (("interval_seconds", "3600"), ("cooldown_seconds", "600")):
            nonce = (await state.get_data())["promotion"]["nonce"]
            await ui.click(f"pp:{nonce}:edit:{field}")
            await ui.message(value)
        nonce = (await state.get_data())["promotion"]["nonce"]
        await ui.click(f"pp:{nonce}:save")
        await ui.click("launch:policy:" + draft_id)
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:check")
        assert "готова" in session.calls[-1].text, session.calls[-1].text
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:commit")
        assert len(r.store.rows("SELECT * FROM outbox WHERE kind='premium_publish'")) == 1
    finally:
        await bot.session.close()


async def test_global_budget_proposal_decline_leaves_policy_untouched(launch):
    """D5: declining the global-change confirmation must not write the policy and
    must not damage the launch draft payload."""
    r = launch
    r.quotes = [
        {"id": "free", "type": "DEFAULT", "price": 0, "period": 7},
        {"id": "premium", "type": "PREMIUM", "price": 10, "period": 7},
    ]
    from dataclasses import asdict

    from app.services.promotion_settings import PromotionSettings
    from app.services.promotions import PromotionPolicy

    ps = PromotionSettings(r.store, r.api)
    rev, pol, sel = ps.get()
    budget = {"per_operation": 50, "daily": 100, "monthly": 1000, "max_operations_per_item": 20}
    full = asdict(PromotionPolicy(**{**asdict(pol), **budget}))
    ps.save(7, rev, full, sel)
    policy_before = ps.get()[1]

    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("launch:item:item")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:mode:text")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:edit:data")
        ui.dp = dispatcher(r.store, r.api)
        await ui.message("Secret delivery text")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:quotes")
        nonce = (await state.get_data())["launch"]["nonce"]
        premium_index = next(i for i, q in enumerate(r.quotes) if q["type"] == "PREMIUM")
        await ui.click(f"ln:{nonce}:quote:{premium_index}")
        nonce = (await state.get_data())["launch"]["nonce"]
        draft_id = (await state.get_data())["launch"]["id"]
        await ui.click(f"ln:{nonce}:edit:schedule")
        nonce = (await state.get_data())["promotion"]["nonce"]
        await ui.click(f"pp:{nonce}:edit:interval_seconds")
        ui.dp = dispatcher(r.store, r.api)
        await ui.message("7200")
        await ui.click("launch:policy:" + draft_id)
        after = ps.get()[1]
        assert asdict(after) == asdict(policy_before)
        assert not (await state.get_data()).get("launch_policy_proposal")
        # The draft payload (delivery data) survived the declined proposal.
        payload = r.store.get_draft(draft_id, 7)["payload"]
        assert payload["data"] == "Secret delivery text"
    finally:
        await bot.session.close()


async def test_dispatcher_configuration_restart_preview_and_final_button(launch):
    r = launch
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("launch:item:item")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:mode:gifts")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:edit:after")
        ui.dp = dispatcher(r.store, r.api)
        await ui.message("Thank you")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:check")
        assert "15" in session.calls[-1].text and "готова" in session.calls[-1].text
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:commit")
        await ui.click(f"ln:{nonce}:commit")
        assert len(r.store.rows("SELECT * FROM outbox WHERE kind='publish'")) == 1
        assert not r.remote.payments and not r.publications
        assert "secret" not in session.calls[-1].text
    finally:
        await bot.session.close()


async def test_service_launch_draft_hidden_from_wizard_and_reopens_after_cancel(launch):
    from app.services.catalog import draft_page

    r = launch
    ident = await r.launch.open(7, "item")
    assert not any(row["id"] == ident for row in draft_page(r.store, 7)["rows"])
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await Interaction(bot, dispatcher(r.store, r.api)).click("draft:" + ident)
        assert await state.get_state() != "wizard"
        assert "Подготовка к публикации" in bot.session.last_panel.text
        r.store.execute("UPDATE drafts SET state='CANCELLED' WHERE id=?", (ident,))
        reopened = await r.launch.open(7, "item")
        assert r.store.get_draft(reopened, 7)["state"] == "DRAFT"
        assert not r.store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()


async def test_cancelled_launch_pointer_does_not_reopen_dead_configuration(launch):
    r = launch
    ident = await r.launch.open(7, "item")
    r.store.execute("UPDATE drafts SET state='CANCELLED' WHERE id=?", (ident,))
    reopened = await r.launch.open(7, "item")
    assert r.store.get_draft(reopened, 7)["state"] == "DRAFT"
    assert not r.store.rows("SELECT * FROM outbox")


async def test_launch_gift_selection_can_undo_clear_and_reject_duplicate_callback(launch):
    from tests.test_ux_redesign import buttons

    r = launch
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("launch:item:item")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:catalog")
        pick = next(k for k in buttons(bot.session.last_panel) if ":gift:" in k)
        await ui.click(pick)
        assert any(k.endswith(":giftundo") for k in buttons(bot.session.last_panel))
        ident = (await state.get_data())["launch"]["id"]
        selected = r.store.get_draft(ident, 7)["payload"]["gift_selection"]
        await ui.click(pick)
        assert r.store.get_draft(ident, 7)["payload"]["gift_selection"] == selected
        await ui.click("launch:open:" + ident)
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:catalog")
        await ui.click(next(k for k in buttons(bot.session.last_panel) if k.endswith(":giftundo")))
        assert r.store.get_draft(ident, 7)["payload"]["gift_selection"] == []
        assert not r.store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()


async def test_gift_binding_keeps_exact_launch_return_after_restart(launch):
    from tests.test_ux_redesign import buttons

    r = launch
    r.store.set_setting("playerok:account_id", r.api.account.id)

    def update_item(id, **changes):
        assert id == "item"
        r.api.item.update(changes)
        return deepcopy(r.api.item)

    r.api.account.update_item = update_item
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("launch:item:item")
        ident = (await state.get_data())["launch"]["id"]
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:catalog")
        await ui.click(next(k for k in buttons(bot.session.last_panel) if ":gift:" in k))
        await ui.click(next(k for k in buttons(bot.session.last_panel) if k.endswith(":savegifts")))
        await ui.click(
            next(k for k in buttons(bot.session.last_panel) if k.startswith("lp:") and k.endswith(":commit"))
        )
        assert "launch:open:" + ident in buttons(bot.session.last_panel)
        assert r.store.get_draft(ident, 7)["payload"].get("gift_binding_operation")
        await r.worker.once()
        ui.dp = dispatcher(r.store, r.api)
        await ui.click("lp:item:item")
        assert "launch:open:" + ident in buttons(bot.session.last_panel)
        await ui.click("launch:open:" + ident)
        assert r.store.get_draft(ident, 7)["payload"]["gift_code"]
        assert not r.remote.payments and not r.publications
    finally:
        await bot.session.close()
