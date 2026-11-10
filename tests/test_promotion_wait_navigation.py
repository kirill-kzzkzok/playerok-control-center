"""Incident recovery through production workers and owner Dispatcher, no paid transport."""

import json

import pytest

from app.playerok.adapter import APIError
from app.services.paid_promotions import intent_key
from app.workers.notifications import Notifications
from tests.test_paid_promotions import enqueue
from tests.test_paid_promotions import rig as rig_fixture

rig = rig_fixture


@pytest.mark.parametrize("automatic", [False, True])
async def test_reserved_premium_waits_for_other_receipt_then_dispatches_once(rig, automatic):
    r = rig
    for item in r.api.items.values():
        item.update(status="APPROVED", priority="PREMIUM")
    if automatic:
        from tests.test_runtime_promotion_isolation import approve_two

        await approve_two(r)
    first = await enqueue(r, key="first", automatic=automatic)
    second = await enqueue(r, "other", "second", automatic=automatic)
    authority = r.store.setting("promotions:paid_authority")
    r.api.bad_receipt = True
    await r.worker.once(operation_id=first)
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (first,))[0]["status"] == "UNCERTAIN"
    await r.worker.once(operation_id=second)
    row = r.store.rows("SELECT * FROM outbox WHERE id=?", (second,))[0]
    assert row["status"] == "RETRYABLE_FAILED"
    assert row["started_at"] is None
    assert len(r.api.charges) == 1
    assert not r.store.setting(intent_key(second))
    assert r.store.rows("SELECT status FROM promotion_expenses WHERE item_id='other'") == [
        {"status": "RESERVED"}
    ]
    # Restart the production worker; only exact read reconciliation unlocks it.
    from tests.test_incident_premium_preflight import restart

    restart(r)
    assert r.store.setting("promotions:paid_authority") == authority
    r.api.bad_receipt = False
    r.api.items["item"]["status_payment"]["status"] = "CONFIRMED"
    await r.service.reconcile_due()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (first,)) == [{"status": "SUCCEEDED"}]
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (second,))
    await r.worker.once(operation_id=second)
    await r.worker.once(operation_id=second)
    assert len(r.api.charges) == 2
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (second,)) == [{"status": "SUCCEEDED"}]


async def test_receipt_wait_does_not_generate_a_generic_failure_warning(rig):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    r = rig
    r.store.set_setting("notifications:initialized", True)
    r.store.set_setting("sync:health", {"status": "OK"})
    r.store.set_setting(
        "promotions:health",
        {"status": "DEGRADED", "incident": "wait", "error_codes": ["PREMIUM_RECONCILIATION_PENDING"]},
    )
    from app.database.store import now

    r.store.execute(
        "INSERT INTO inbox(key,kind,payload,received_at,processed_at) VALUES(?,?,?,?,?)",
        ("promotion_error:wait", "promotion_error", json.dumps({"incident": "wait"}), now(), now()),
    )
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)))
    await Notifications(r.store, bot).once()
    bot.send_message.assert_not_called()


@pytest.mark.parametrize("proof", ["started", "late", "intent"])
async def test_started_or_late_payment_cannot_use_wait_retry(rig, proof):
    r = rig
    r.api.items["item"].update(status="APPROVED", priority="PREMIUM")
    op = await enqueue(r)
    original = r.worker.executor

    async def started(action):
        if proof == "started":
            r.store.execute("UPDATE outbox SET started_at=? WHERE id=?", ("2026-11-10T00:00:00+00:00", op))
        elif proof == "late":
            r.store.record_late_result(op, "previous-lease", "UNCERTAIN", error="LATE_RESULT")
        else:
            r.store.set_setting(intent_key(op), {"started_at": "2026-11-10T00:00:00+00:00"})
        raise APIError("PREMIUM_RECONCILIATION_PENDING")

    r.worker.executor = started
    await r.worker.once(operation_id=op)
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "UNCERTAIN"
    r.worker.executor = original
    assert not r.api.charges
