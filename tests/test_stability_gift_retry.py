"""Only conclusively unstarted Gift reads may retry, using actual UI and workers."""

import asyncio

import pytest

from app.gifts.worker import GiftWorker
from app.services.orders import Orders
from tests.test_stability_deal_completeness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui


@pytest.fixture
async def shop(tmp_path):
    async for create in shop_fixture.__wrapped__(tmp_path):
        yield create


async def pending(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    Orders(r.store).ingest(r.api.order("free-check"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    return r


async def test_network_read_retry_rechecks_then_pays_exactly_once(shop):
    r = await pending(shop)
    r.api.remote.resolve_error = TimeoutError()
    await r.worker.once()
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE kind='gift_send'") == [
        {"status": "RETRYABLE_FAILED", "started_at": None}
    ]
    assert r.store.rows("SELECT state FROM gift_steps") == [{"state": "PENDING"}]
    assert not r.api.remote.payments
    assert not await r.worker.once()  # backoff, no busy loop
    r.api.remote.resolve_error = None
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE kind='gift_send'")
    await asyncio.gather(r.worker.once(), r.worker.once())
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    assert len(r.api.remote.payments) == 1
    assert r.store.rows("SELECT started_at FROM outbox WHERE kind='gift_send'")[0]["started_at"]


async def test_free_read_retries_are_bounded_and_never_pay(shop):
    r = await pending(shop)
    r.api.remote.resolve_error = TimeoutError()
    for _ in range(6):
        r.store.execute("UPDATE outbox SET retry_at=0 WHERE kind='gift_send'")
        await r.worker.once()
    assert r.store.rows("SELECT attempts,status FROM outbox WHERE kind='gift_send'") == [
        {"attempts": 4, "status": "PERMANENT_FAILED"}
    ]
    assert not r.api.remote.payments


async def test_interrupted_free_check_recovery_fences_old_worker(shop):
    r = await pending(shop)
    old = r.store.claim()
    r.store.execute("UPDATE gift_steps SET state='CHECKING'")
    r.store.execute("UPDATE gift_jobs SET state='CHECKING'")
    r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (old["id"],))
    r.store.recover()
    assert r.store.rows("SELECT status FROM outbox WHERE kind='gift_send'") == [
        {"status": "RETRYABLE_FAILED"}
    ]
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE kind='gift_send'")
    await r.worker.once()
    with pytest.raises(ValueError):
        r.worker.executor.store.mark_started(old)
    assert len(r.api.remote.payments) == 1


@pytest.mark.parametrize("fault", ["bug", "paid_timeout"])
async def test_bug_and_unknown_payment_are_not_free_retry_authority(shop, fault):
    r = await pending(shop)
    if fault == "bug":
        r.api.remote.resolve_error = RuntimeError("test-only programming failure")
    else:
        r.api.remote.pay_error = TimeoutError()
    await r.worker.once()
    expected = "PERMANENT_FAILED" if fault == "bug" else "UNCERTAIN"
    assert r.store.rows("SELECT status FROM outbox WHERE kind='gift_send'") == [{"status": expected}]
    for _ in range(3):
        await r.worker.once()
    assert len(r.api.remote.payments) == int(fault == "paid_timeout")


async def test_stop_during_free_backoff_cannot_be_bypassed(shop):
    r = await pending(shop)
    r.api.remote.resolve_error = TimeoutError()
    await r.worker.once()
    r.store.set_setting("gifts:stopped", True)
    r.api.remote.resolve_error = None
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE kind='gift_send'")
    await r.worker.once()
    assert not r.api.remote.payments
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "BLOCKED"}]


async def test_flood_wait_is_respected_without_spending(shop):
    import time

    from telethon import errors

    r = await pending(shop)
    r.api.remote.resolve_error = errors.FloodWaitError(None, 120)
    before = time.time()
    await r.worker.once()
    row = r.store.rows("SELECT status,retry_at FROM outbox WHERE kind='gift_send'")[0]
    assert row["status"] == "RETRYABLE_FAILED" and row["retry_at"] >= before + 120
    assert not await r.worker.once()
    assert not r.api.remote.payments


async def test_graceful_restart_during_free_resolve_retains_safe_retry(shop):
    from telethon import functions

    r = await pending(shop)
    entered = asyncio.Event()
    transport = r.api.gift_sender.client

    class WaitingTransport:
        def __getattr__(self, name):
            return getattr(transport, name)

        async def __call__(self, request):
            if isinstance(request, functions.contacts.ResolveUsernameRequest):
                entered.set()
                await asyncio.Event().wait()
            return await transport(request)

    r.api.gift_sender.client = WaitingTransport()
    task = asyncio.create_task(r.worker.once())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE kind='gift_send'") == [
        {"status": "RETRYABLE_FAILED", "started_at": None}
    ]
    r.api.gift_sender.client = transport
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE kind='gift_send'")
    await r.worker.once()
    assert len(r.api.remote.payments) == 1
