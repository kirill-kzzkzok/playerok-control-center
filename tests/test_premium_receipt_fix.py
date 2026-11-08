"""Real paid worker/ledger and reconciliation; only marketplace boundary simulated."""

import asyncio
import copy
import json
from datetime import datetime, timedelta

import pytest

from app.database.store import Conflict, Store
from app.playerok.adapter import APIError
from app.services.account_binding import bind_account
from app.services.paid_promotions import intent_key
from app.services.reconciliation import Reconciliation, signature
from app.workers.promotions import PromotionWorker
from tests.test_paid_promotions import enqueue
from tests.test_paid_promotions import rig as rig_fixture

rig = rig_fixture


async def scenario(r, *, mutation="old", lag=0, fault=None):
    bind_account(r.store, "seller")
    r.api.items["item"].update(status="APPROVED", priority="PREMIUM", status_payment={"id": "old"})
    op = await enqueue(r)
    original_charge = r.api.charge
    original_call = r.api.call
    reads = []

    def charge(**kwargs):
        result = original_charge(**kwargs)
        receipt = result["status_payment"]
        intent = r.store.setting(intent_key(op))
        r.api.wallet = [
            {**copy.deepcopy(receipt), "user": {"id": "seller"}, "created_at": intent["started_at"]}
        ]
        if fault == "owner":
            r.api.wallet[0]["user"] = {"id": "another"}
        if fault == "amount":
            r.api.wallet[0]["value"] = 11
        if fault == "direction":
            r.api.wallet[0]["direction"] = "IN"
        if fault == "time":
            r.api.wallet[0]["created_at"] = (
                datetime.fromisoformat(intent["started_at"]) + timedelta(minutes=2)
            ).isoformat()
        if mutation == "lost":
            raise TimeoutError()
        if mutation == "old":
            result["status_payment"] = {"id": "old"}
        elif mutation == "missing":
            result.pop("status_payment")
        return result

    async def call(method, **kwargs):
        if method == "get_item" and r.api.charges:
            reads.append(method)
            if fault == "read":
                raise APIError("NETWORK_ERROR")
            result = await original_call(method, **kwargs)
            if len(reads) <= lag:
                result["status_payment"] = {"id": "old"}
            if fault == "item":
                result["id"] = "other"
            if fault == "item_owner":
                result["user"] = {"id": "another"}
            return result
        return await original_call(method, **kwargs)

    r.api.account.increase_item_priority_status = charge
    r.api.call = call
    return op, reads


@pytest.mark.parametrize("mutation,lag", [("full", 0), ("old", 0), ("missing", 0), ("old", 2), ("full", 1)])
async def test_successful_bump_confirms_exact_new_receipt_without_another_post(rig, mutation, lag):
    r = rig
    op, reads = await scenario(r, mutation=mutation, lag=lag)
    await r.worker.once(operation_id=op)
    row = r.store.rows("SELECT * FROM outbox WHERE id=?", (op,))[0]
    assert row["status"] == "SUCCEEDED", row["error_code"]
    assert json.loads(row["result"])["payment_id"] == "receipt-1"
    assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "SUCCEEDED"}]
    assert r.store.setting(intent_key(op))["receipt_id"] == "receipt-1"
    await r.worker.once(operation_id=op)
    assert len(r.api.charges) == 1 and len(reads) <= 4


@pytest.mark.parametrize("fault", ["owner", "amount", "direction", "time", "item", "item_owner", "read"])
async def test_nonmatching_or_unavailable_evidence_stays_unknown(rig, fault):
    r = rig
    op, _ = await scenario(r, fault=fault)
    await r.worker.once(operation_id=op)
    row = r.store.rows("SELECT * FROM outbox WHERE id=?", (op,))[0]
    assert row["status"] == "UNCERTAIN"
    assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "UNCERTAIN"}]
    await r.worker.once(operation_id=op)
    assert len(r.api.charges) == 1
    diagnostic = r.store.setting(intent_key(op)).get("diagnostic", {})
    assert diagnostic.get("stage") in {"READBACK", "RECEIPT_VALIDATION"}
    assert diagnostic.get("reason") and isinstance(diagnostic.get("candidate_present"), bool)


async def test_history_fallback_uses_mutation_candidate_when_item_stays_old(rig):
    r = rig
    op, _ = await scenario(r, mutation="full", lag=10)
    await r.worker.once(operation_id=op)
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "SUCCEEDED"}]
    assert len(r.api.charges) == 1


async def test_lost_response_never_guesses_payment_from_current_item(rig):
    r = rig
    op, _ = await scenario(r, mutation="lost")
    await r.worker.once(operation_id=op)
    await PromotionWorker(r.store, r.api).once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "UNCERTAIN"}]
    assert len(r.api.charges) == 1
    assert r.store.setting(intent_key(op))["diagnostic"]["stage"] == "MUTATION_RESPONSE"


async def test_candidate_survives_failed_history_read_before_validation(rig):
    r = rig
    op, _ = await scenario(r)
    original = r.api.call

    async def fail(method, **kwargs):
        if method == "get_transactions":
            raise APIError("HTTP_503")
        return await original(method, **kwargs)

    r.api.call = fail
    await r.worker.once(operation_id=op)
    intent = r.store.setting(intent_key(op))
    assert intent["candidate_id"] == "receipt-1"
    assert intent["diagnostic"]["stage"] == "READBACK"
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "UNCERTAIN"}]


async def test_saved_item_anchor_survives_a_later_item_receipt(rig):
    r = rig
    op, _ = await scenario(r)
    original = r.api.call

    async def offline_history(method, **kwargs):
        if method == "get_transactions":
            raise APIError("HTTP_503")
        return await original(method, **kwargs)

    r.api.call = offline_history
    await r.worker.once(operation_id=op)
    intent = r.store.setting(intent_key(op))
    assert intent["candidate_id"] == "receipt-1"
    # A subsequent placement replaces the item's latest receipt. The saved
    # account/item anchor and exact history ID must still prove the OLD debit.
    r.api.items["item"]["status_payment"]["id"] = "later-receipt"
    r.api.call = type(r.api).call.__get__(r.api)
    row = r.store.rows("SELECT * FROM outbox WHERE id=?", (op,))[0]
    proof = await r.service.reconcile(row)
    assert proof["payment_id"] == "receipt-1"
    assert r.store.setting(intent_key(op))["candidate_id"] == "receipt-1"
    assert len(r.api.charges) == 1


async def test_future_unknown_confirms_after_restart_with_stop_still_in_force(rig):
    r = rig
    op, _ = await scenario(r, fault="read")
    await r.worker.once(operation_id=op)
    path = r.store.path
    restored = Store(path)
    try:
        # Disable automatic purchases; confirming a previously spent receipt
        # must not re-arm the policy or send another POST.
        from dataclasses import asdict

        rev, policy, selected = r.settings.get()
        r.settings.save(7, rev, {**asdict(policy), "enabled": False}, selected)
        r.api.call = type(r.api).call.__get__(r.api)
        await PromotionWorker(restored, r.api).once()
        assert restored.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "SUCCEEDED"}]
        assert not r.settings.get()[1].enabled and len(r.api.charges) == 1
    finally:
        restored.close()


async def test_legacy_reconciliation_does_not_write_during_evidence_prepare(rig):
    r = rig
    op, _ = await scenario(r, fault="read")
    await r.worker.once(operation_id=op)
    intent = r.store.setting(intent_key(op))
    # Represent the old release's terminal journal (no new-version marker).
    r.store.set_setting(
        intent_key(op),
        {"operation_id": op, "started_at": intent["started_at"], "candidate_id": "old", "pending": True},
    )
    r.api.call = type(r.api).call.__get__(r.api)
    before = r.store.setting(intent_key(op))
    row = r.store.rows("SELECT * FROM outbox WHERE id=?", (op,))[0]
    evidence = await r.service.reconcile(row)
    assert evidence["payment_id"] == "receipt-1"
    assert r.store.setting(intent_key(op)) == before
    assert row["status"] == "UNCERTAIN"
    await PromotionWorker(r.store, r.api).once()
    assert r.store.setting(intent_key(op)) == before
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "UNCERTAIN"}]


async def test_restart_after_receipt_before_worker_finish_confirms_without_post(rig):
    r = rig
    op, _ = await scenario(r, mutation="full")
    action = r.store.claim(operation_id=op)
    result = await r.worker.executor(action)
    assert result["payment_id"] == "receipt-1"
    assert r.store.setting(intent_key(op))["receipt_id"] == "receipt-1"
    r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (op,))
    r.api.read_failure = True  # Previously validated receipt survives a later read outage.
    restored = Store(r.store.path)
    try:
        restored.recover()
        await PromotionWorker(restored, r.api).once()
        assert restored.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "SUCCEEDED"}]
        assert restored.rows("SELECT status FROM promotion_expenses") == [{"status": "SUCCEEDED"}]
        assert len(r.api.charges) == 1
    finally:
        restored.close()


async def test_different_later_receipt_never_replaces_valid_mutation_candidate(rig):
    r = rig
    op, _ = await scenario(r, mutation="full")
    original = r.api.call

    async def replaced(method, **kwargs):
        result = await original(method, **kwargs)
        if r.api.charges and method == "get_item":
            result["status_payment"]["id"] = "receipt-later"
        if method == "get_transactions":
            result["transactions"][0]["id"] = "receipt-later"
        return result

    r.api.call = replaced
    await r.worker.once(operation_id=op)
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "UNCERTAIN"}]
    assert r.store.setting(intent_key(op))["candidate_id"] == "receipt-1"
    assert len(r.api.charges) == 1


async def test_concurrent_reconciliations_cannot_claim_one_receipt_twice(rig):
    r = rig
    first, _ = await scenario(r, mutation="full", fault="read")
    await r.worker.once(operation_id=first)
    r.api.call = type(r.api).call.__get__(r.api)
    original = r.store.rows("SELECT * FROM outbox WHERE id=?", (first,))[0]
    r.api.items["other"] = {**copy.deepcopy(r.api.items["item"]), "id": "other"}
    payload = json.loads(original["payload"])
    payload["item"] = {**payload["item"], "id": "other"}
    second = r.store.enqueue("legacy-conflicting-op", "premium_bump", "other", payload)
    action = r.store.claim(operation_id=second)
    r.store.mark_started(action)
    r.store.set_setting(intent_key(second), {**r.store.setting(intent_key(first)), "operation_id": second})
    r.store.execute(
        "INSERT INTO promotion_expenses(operation_key,item_id,amount,status,created_at) SELECT 'legacy-conflicting-op','other',amount,'UNCERTAIN',created_at FROM promotion_expenses WHERE operation_key=?",
        (original["operation_key"],),
    )
    r.store.finish(second, action["lease_token"], "UNCERTAIN", error="PREMIUM_PAYMENT_RESULT_UNKNOWN")
    service = Reconciliation(r.store, r.api)
    evidence = service.evidence
    barrier = asyncio.Event()
    ready = 0

    async def synchronized(*args, **kwargs):
        nonlocal ready
        result = await evidence(*args, **kwargs)
        ready += 1
        if ready == 2:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), 3)
        return result

    service.evidence = synchronized
    result = await asyncio.gather(
        *(
            service.resolve(
                7,
                ident,
                signature(service.action(7, ident)),
                "CONFIRM_READ_STATE",
                "Проверка точного серверного списания",
            )
            for ident in (first, second)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(row, dict) for row in result) == 1, result
    assert sum(isinstance(row, (Conflict, APIError)) for row in result) == 1
    assert len(r.store.rows("SELECT id FROM outbox WHERE status='SUCCEEDED'")) == 1
    assert len(r.api.charges) == 1


async def test_persistence_failure_after_receipt_keeps_unknown_and_exact_candidate(rig, monkeypatch):
    r = rig
    op, _ = await scenario(r)
    original = r.store.set_setting
    failed = False

    def storage(key, value):
        nonlocal failed
        if key == intent_key(op) and value.get("receipt_id") and not failed:
            failed = True
            raise OSError("sensitive storage diagnostic must not escape")
        return original(key, value)

    monkeypatch.setattr(r.store, "set_setting", storage)
    await r.worker.once(operation_id=op)
    intent = r.store.setting(intent_key(op))
    assert intent["candidate_id"] == "receipt-1" and intent["diagnostic"]["stage"] == "PERSISTENCE"
    assert "sensitive" not in json.dumps(intent)
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "UNCERTAIN"}]
    assert len(r.api.charges) == 1


def test_sdk_retains_old_mutation_receipt_and_reads_new_item_receipt_without_loss():
    import httpx

    from app.playerok.adapter import SafeAccount, plain

    old = {
        "id": "old",
        "operation": "ITEM_PREMIUM_PRIORITY",
        "direction": "OUT",
        "providerId": "LOCAL",
        "status": "CONFIRMED",
        "value": 9,
    }
    item = {
        "__typename": "MyItem",
        "id": "lot",
        "status": "APPROVED",
        "priority": "PREMIUM",
        "user": {"id": "seller"},
        "statusPayment": old,
    }

    def respond(request):
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"increaseItemPriorityStatus": item}})
        return httpx.Response(200, json={"data": {"item": {**item, "statusPayment": {**old, "id": "new"}}}})

    account = SafeAccount(
        "token=test", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    try:
        with account.permit("increaseItemPriorityStatus"):
            result = plain(account.increase_item_priority_status("lot", "quote"))
        current = plain(account.get_item("lot"))
        assert result["status_payment"]["id"] == "old" and current["status_payment"]["id"] == "new"
        assert current["status_payment"]["provider_id"] == "LOCAL"
        assert current["status_payment"]["value"] == 9
    finally:
        account.close()


async def test_diagnostic_write_cannot_precede_saving_returned_candidate(rig, monkeypatch):
    r = rig
    op, _ = await scenario(r, mutation="full")
    original = r.store.set_setting

    def storage(key, value):
        if (
            key == intent_key(op)
            and value.get("diagnostic", {}).get("stage") == "CANDIDATE_EXTRACTION"
            and "candidate_id" not in value
        ):
            raise OSError("a diagnostic must not lose an already returned candidate")
        return original(key, value)

    monkeypatch.setattr(r.store, "set_setting", storage)
    await r.worker.once(operation_id=op)
    assert r.store.setting(intent_key(op))["candidate_id"] == "receipt-1"
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "SUCCEEDED"}]


@pytest.mark.parametrize("mutation", ["full", "old"])
async def test_partial_item_receipt_can_be_proved_by_exact_history(rig, mutation):
    r = rig
    op, _ = await scenario(r, mutation=mutation)
    original = r.api.call

    async def partial(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_item" and r.api.charges:
            result["status_payment"] = {"id": "receipt-1"}
        return result

    r.api.call = partial
    await r.worker.once(operation_id=op)
    row = r.store.rows("SELECT status,result FROM outbox WHERE id=?", (op,))[0]
    assert row["status"] == "SUCCEEDED"
    assert json.loads(row["result"])["payment_id"] == "receipt-1"
    assert len(r.api.charges) == 1


async def test_matching_item_receipt_dated_before_dispatch_stays_unknown(rig):
    r = rig
    op, _ = await scenario(r, mutation="full")
    original = r.api.call

    async def stale(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_item" and r.api.charges:
            started = r.store.setting(intent_key(op))["started_at"]
            result["status_payment"]["created_at"] = (
                datetime.fromisoformat(started) - timedelta(days=1)
            ).isoformat()
        return result

    r.api.call = stale
    await r.worker.once(operation_id=op)
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "UNCERTAIN"}]
    assert len(r.api.charges) == 1
