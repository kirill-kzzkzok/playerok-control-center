"""Empty public caption; durable exact Telegram acknowledgement, never guesses."""

import asyncio
import copy

import pytest
from telethon import types

from app.database.store import Store
from app.gifts.mtproto import GiftFailure
from app.gifts.service import GiftService
from app.services.orders import Orders
from app.services.products import Products
from app.workers.outbox import Executor, OutboxWorker
from tests.test_gifts import enroll
from tests.test_gifts import rig as rig_fixture


@pytest.fixture(name="rig")
def _rig(tmp_path):
    yield from rig_fixture.__wrapped__(tmp_path)


async def test_public_gift_has_no_message_but_internal_reference_remains(rig):
    r = rig
    await enroll(r)
    await r.worker.once()
    request = r.remote.payments[0]
    assert request.invoice.message is None
    assert b"PCC-" not in bytes(request)
    step = r.service.steps("order")[0]
    assert step["reference"].startswith("PCC-")
    assert step["state"] == "SUCCEEDED"
    assert step["ack_message_id"] == step["message_id"] == 1
    assert r.remote.messages[0].action.message is None


async def test_ack_is_saved_before_independent_history_read_and_survives_restart(rig):
    r = rig
    await enroll(r)

    async def timeout(peer, **kwargs):
        assert r.service.steps("order")[0]["ack_message_id"] == 1
        raise TimeoutError()

    original = r.remote.get_messages
    r.remote.get_messages = timeout
    await r.worker.once()
    assert r.service.steps("order")[0]["ack_message_id"] == 1
    assert r.service.job("order")["state"] == "UNCERTAIN"
    assert len(r.remote.payments) == 1
    r.remote.get_messages = original
    path = r.store.path
    r.store.close()
    r.store = Store(path)
    r.service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    r.worker = OutboxWorker(r.store, Executor(r.store, r.api, Products(r.store, r.api)))
    await r.service.reconcile(7, "order")
    assert r.service.job("order")["state"] == "SUCCEEDED"
    assert len(r.remote.payments) == 1 and await r.worker.once() is True  # notice only
    r.store.close()


async def test_multiple_ack_candidates_are_never_guessed(rig):
    r = rig
    await enroll(r)
    original = r.api.gift_sender.pay

    async def ambiguous(prepared):
        result = await original(prepared)
        message = copy.deepcopy(result.updates.updates[0].message)
        message.id += 100
        result.updates.updates.append(types.UpdateNewMessage(message, 2, 1))
        return result

    r.api.gift_sender.pay = ambiguous
    await r.worker.once()
    assert r.service.job("order")["state"] == "UNCERTAIN"
    assert r.service.steps("order")[0]["ack_message_id"] is None
    assert len(r.remote.payments) == 1
    with pytest.raises(GiftFailure):
        await r.service.reconcile(7, "order")


async def test_missing_rpc_message_id_cannot_adopt_even_a_single_history_candidate(rig):
    r = rig
    await enroll(r)
    original = r.api.gift_sender.pay

    async def no_updates(prepared):
        result = await original(prepared)
        result.updates.updates = []
        return result

    r.api.gift_sender.pay = no_updates
    await r.worker.once()
    assert r.service.job("order")["state"] == "UNCERTAIN"
    assert len(r.remote.payments) == 1
    with pytest.raises(GiftFailure):
        await r.service.reconcile(7, "order")
    assert len(r.remote.payments) == 1


async def test_identical_gifts_same_recipient_have_distinct_exact_receipts(rig):
    r = rig
    await enroll(r)
    await enroll(r, ident="second")
    # Both paid events have arrived before either is processed.
    await asyncio.gather(r.worker.once(), r.worker.once())
    for ident in ("order", "second"):
        if r.service.job(ident)["state"] == "BLOCKED":
            r.service.retry_preflight(7, ident)
    while await r.worker.once():
        pass
    steps = r.store.rows("SELECT * FROM gift_steps ORDER BY order_id")
    assert len(r.remote.payments) == 2
    assert all(g["state"] == "SUCCEEDED" for g in steps)
    assert len({g["message_id"] for g in steps}) == 2
    assert all(q.invoice.message is None for q in r.remote.payments)
    assert len({g["reference"] for g in steps}) == 2


async def test_second_order_cannot_claim_first_orders_ack(rig):
    r = rig
    await enroll(r)
    await r.worker.once()
    await enroll(r, ident="second")
    original = r.api.gift_sender.pay

    async def wrong_ack(prepared):
        result = await original(prepared)
        result.updates.updates[0].message = copy.deepcopy(r.remote.messages[0])
        return result

    r.api.gift_sender.pay = wrong_ack
    await r.worker.once()
    assert r.service.job("order")["state"] == "SUCCEEDED"
    assert r.service.job("second")["state"] == "UNCERTAIN"
    assert len(r.remote.payments) == 2
    assert not r.api.refunds


async def test_lost_ack_stays_unknown_without_visible_marker_or_repeat(rig):
    r = rig
    await enroll(r)
    r.remote.pay_error = TimeoutError()
    await r.worker.once()
    assert r.remote.payments[0].invoice.message is None
    assert r.service.job("order")["state"] == "UNCERTAIN"
    with pytest.raises(GiftFailure):
        await r.service.reconcile(7, "order")
    Orders(r.store).ingest(r.api.orders["order"])
    assert r.service.discover() == 0
    assert await r.worker.once() is False
    assert len(r.remote.payments) == 1


async def test_legacy_reference_receipt_remains_read_only_and_valid(rig):
    from datetime import UTC, datetime, timedelta

    r = rig
    legacy = {
        "recipient_id": 55,
        "payer_id": 99,
        "recipient_access_hash": "987",
        "gift_id": str(r.remote.gift.id),
        "reference": "PCC-legacy-reference",
        "spent_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        "receipt_mode": "REFERENCE",
        "ack_message_id": None,
    }
    r.remote.messages.append(
        types.MessageService(
            id=77,
            peer_id=types.PeerUser(55),
            from_id=types.PeerUser(99),
            out=True,
            date=datetime.now(UTC),
            action=types.MessageActionStarGift(
                r.remote.gift, message=types.TextWithEntities(legacy["reference"], [])
            ),
        )
    )
    receipt = await r.api.gift_sender.receipt(legacy)
    assert receipt["message_id"] == 77 and receipt["reference"] == legacy["reference"]
    assert not r.remote.payments


async def test_crash_before_ack_commit_never_adopts_history_or_repeats(rig):
    import sqlite3

    r = rig
    await enroll(r)
    r.store.execute(
        "CREATE TEMP TRIGGER fail_ack BEFORE UPDATE OF ack_message_id ON gift_steps "
        "BEGIN SELECT RAISE(ABORT,'injected ack commit failure'); END"
    )
    try:
        await r.worker.once()
    except sqlite3.Error:
        pass
    assert len(r.remote.payments) == 1
    assert r.service.steps("order")[0]["ack_message_id"] is None
    r.store.execute("DROP TRIGGER fail_ack")
    r.store.execute("UPDATE outbox SET lease_until=0 WHERE kind='gift_send'")
    r.store.recover()
    assert r.service.job("order")["state"] == "UNCERTAIN"
    with pytest.raises(GiftFailure):
        await r.service.reconcile(7, "order")
    assert await r.worker.once() is False and len(r.remote.payments) == 1


@pytest.mark.parametrize(
    "stage,fault",
    [
        ("ack", "sender"),
        ("ack", "time"),
        ("history", "sender"),
        ("history", "time"),
        ("history", "message_id"),
    ],
)
async def test_exact_ack_and_independent_receipt_reject_wrong_identity_or_time(rig, stage, fault):
    from datetime import UTC, datetime, timedelta

    r = rig
    await enroll(r)

    def corrupt(message):
        if fault == "sender":
            message.from_id = types.PeerUser(1000)
        elif fault == "time":
            message.date = datetime.now(UTC) - timedelta(hours=1)
        else:
            message.id += 1000

    if stage == "ack":
        original = r.api.gift_sender.pay

        async def pay(prepared):
            result = await original(prepared)
            corrupt(result.updates.updates[0].message)
            return result

        r.api.gift_sender.pay = pay
    else:
        original_read = r.remote.get_messages

        async def read(peer, **kwargs):
            messages = copy.deepcopy(await original_read(peer, **kwargs))
            for message in messages:
                corrupt(message)
            return messages

        r.remote.get_messages = read
    await r.worker.once()
    assert r.service.job("order")["state"] == "UNCERTAIN"
    assert not r.service.steps("order")[0]["receipt"]
    assert len(r.remote.payments) == 1
    assert await r.worker.once() is False
    assert not r.api.refunds and not r.api.sent
