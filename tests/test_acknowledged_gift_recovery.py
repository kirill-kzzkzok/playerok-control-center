"""A durable payment ACK permits an exact receipt read, never another payment."""

import json

import pytest

from app.gifts.worker import GiftWorker
from app.services.orders import Orders
from tests.test_logical_faults import reopen
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


async def unknown_after_ack(r, codes="heart", *, completion=False):
    await setup_ui(r, "telegram_gifts", "gift: " + codes, completion=completion)
    Orders(r.store).ingest(r.api.order("paid"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    read = r.api.remote.get_messages

    async def unavailable(*args, **kwargs):
        raise TimeoutError

    r.api.remote.get_messages = unavailable
    await r.worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "UNCERTAIN"}]
    step = r.store.rows("SELECT * FROM gift_steps")[0]
    assert step["ack_message_id"] and step["receipt_mode"] == "MESSAGE_ID"
    r.api.remote.get_messages = read
    return step


@pytest.mark.parametrize("restart", [False, True])
async def test_acknowledged_payment_is_verified_in_background_without_telegram_click_or_second_payment(
    shop, restart
):
    r = await shop(gift=True)
    step = await unknown_after_ack(r)
    if restart:
        reopen(r)
    worker = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    op = r.store.rows("SELECT status,result FROM outbox WHERE id=?", (step["operation_id"],))[0]
    assert op["status"] == "SUCCEEDED"
    assert json.loads(op["result"])["gift_receipt"]["message_id"] == step["ack_message_id"]
    await worker.once()
    assert len(r.api.remote.payments) == 1


async def test_receipt_verification_never_clears_owner_stop(shop):
    r = await shop(gift=True)
    await unknown_after_ack(r)
    r.store.set_setting("gifts:stopped", True)
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    assert r.store.setting("gifts:stopped") is True
    await r.worker.once()
    assert len(r.api.remote.payments) == 1
    assert r.api.orders["paid"]["status"] == "PAID"


@pytest.mark.parametrize("risk", ["no_ack", "missing", "recipient", "gift", "payer", "late"])
async def test_no_evidence_or_conflicting_receipt_stays_unknown_and_never_pays_again(shop, risk):
    from telethon import types

    r = await shop(gift=True)
    step = await unknown_after_ack(r)
    if risk == "no_ack":
        r.store.execute("UPDATE gift_steps SET ack_message_id=NULL")
    elif risk == "missing":
        r.api.remote.messages.clear()
    elif risk == "recipient":
        r.api.remote.messages[0].peer_id = types.PeerUser(98765)
    elif risk == "gift":
        r.api.remote.messages[0].action.gift.id += 1
    elif risk == "payer":
        r.api.gift_sender.me = types.User(id=98765)
    else:
        r.store.record_late_result(step["operation_id"], "old-lease", "UNCERTAIN", error="LATE_CONFLICT")
    worker = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await worker.once()
    await worker.once()
    await r.worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "UNCERTAIN"}]
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (step["operation_id"],)) == [
        {"status": "UNCERTAIN"}
    ]
    assert len(r.api.remote.payments) == 1


async def test_receipt_transaction_rolls_back_outbox_and_step_together(shop, monkeypatch):
    import sqlite3

    r = await shop(gift=True)
    step = await unknown_after_ack(r)
    worker = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    original = worker.service.continue_bundle

    def broken(*args):
        raise sqlite3.OperationalError("isolated commit failure")

    monkeypatch.setattr(worker.service, "continue_bundle", broken)
    with pytest.raises(sqlite3.OperationalError):
        await worker.once()
    assert r.store.rows("SELECT state FROM gift_steps") == [{"state": "UNCERTAIN"}]
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (step["operation_id"],)) == [
        {"status": "UNCERTAIN"}
    ]
    monkeypatch.setattr(worker.service, "continue_bundle", original)
    r.store.execute("UPDATE outbox SET retry_at=0")
    await worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    assert len(r.api.remote.payments) == 1


async def test_partial_bundle_receipt_recovers_then_remaining_guarded_payment_runs_once(shop):
    r = await shop(gift=True)
    await unknown_after_ack(r, "bear, rose")
    worker = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await worker.once()
    assert len(r.api.remote.payments) == 1
    assert [s["state"] for s in worker.service.steps("paid")] == ["SUCCEEDED", "PENDING"]
    await r.worker.once()
    await worker.once()
    assert len(r.api.remote.payments) == 2
    assert [s["state"] for s in worker.service.steps("paid")] == ["SUCCEEDED", "SUCCEEDED"]
    assert r.store.rows("SELECT status FROM outbox WHERE kind='gift_send'") == [
        {"status": "SUCCEEDED"},
        {"status": "SUCCEEDED"},
    ]


async def test_verified_ack_runs_original_authorized_completion_without_another_payment(shop):
    r = await shop(gift=True)
    await unknown_after_ack(r, completion=True)
    worker = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    for _ in range(8):
        await worker.once()
        await r.worker.once()
    assert len(r.api.remote.payments) == 1
    assert r.api.orders["paid"]["status"] == "SENT"
    assert r.store.rows("SELECT status FROM outbox WHERE kind='fulfill'") == [{"status": "SUCCEEDED"}]


@pytest.mark.parametrize("change", ["cancel", "closed"])
async def test_receipt_wait_cannot_overwrite_concurrent_resolution_or_cancelled_worker(shop, change):
    import asyncio

    r = await shop(gift=True)
    step = await unknown_after_ack(r)
    reading, release = asyncio.Event(), asyncio.Event()
    original = r.api.remote.get_messages

    async def delayed(*args, **kwargs):
        reading.set()
        await release.wait()
        return await original(*args, **kwargs)

    r.api.remote.get_messages = delayed
    worker = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    task = asyncio.create_task(worker.once())
    try:
        await asyncio.wait_for(reading.wait(), 2)
        if change == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            r.store.execute(
                "UPDATE outbox SET status='PERMANENT_FAILED',error_code='MANUALLY_STOPPED_UNCERTAIN' WHERE id=?",
                (step["operation_id"],),
            )
            release.set()
            await task
        assert r.store.rows("SELECT state FROM gift_steps") == [{"state": "UNCERTAIN"}]
        assert len(r.api.remote.payments) == 1
        expected = "UNCERTAIN" if change == "cancel" else "PERMANENT_FAILED"
        assert r.store.rows("SELECT status FROM outbox WHERE id=?", (step["operation_id"],)) == [
            {"status": expected}
        ]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
