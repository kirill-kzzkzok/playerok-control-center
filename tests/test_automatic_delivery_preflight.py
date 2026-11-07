"""Real paid stock/text chains recover only before their mutation started."""

import asyncio

import pytest

from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.telegram.home_panel import HomePanel
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_logical_faults import reopen
from tests.test_stage_a_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui

shop = shop_fixture


async def prepare(shop, mode):
    r = await shop()
    await setup_ui(r, mode, "isolated fixed payload")
    Orders(r.store).ingest(r.api.order("waiting-paid"))
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    return r, r.store.rows("SELECT id FROM outbox WHERE target='waiting-paid'")[0]["id"]


@pytest.mark.parametrize("mode", ["fixed_text", "unique_stock"])
async def test_five_free_read_failures_keep_same_operation_and_recover_once_after_restart(shop, mode):
    r, ident = await prepare(shop, mode)
    original = r.api.call
    attempts = 0

    async def unavailable(method, **kwargs):
        nonlocal attempts
        if method == "get_item" and attempts < 5:
            attempts += 1
            raise APIError("HTTP_503")
        return await original(method, **kwargs)

    r.api.call = unavailable
    for index in range(5):
        await r.worker.once()
        row = r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (ident,))[0]
        assert row == {"status": "RETRYABLE_FAILED", "started_at": None}
        assert not r.api.messages
        if index == 2:
            reopen(r)
        r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (ident,))
    for _ in range(8):
        await r.worker.once()
        ChainWorker(r.store, r.api).once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "SUCCEEDED"}]
    assert len(r.api.messages) == 2
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "SUCCEEDED"}]
    if mode == "unique_stock":
        assert r.store.rows("SELECT COUNT(*) AS n FROM inventory WHERE status='DELIVERED'") == [{"n": 1}]


async def test_six_unstarted_lease_expirations_do_not_require_owner_restart(shop):
    r, ident = await prepare(shop, "unique_stock")
    for _ in range(6):
        claimed = r.store.claim(operation_id=ident)
        assert claimed
        r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (ident,))
        r.store.recover()
        assert r.store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [
            {"status": "RETRYABLE_FAILED"}
        ]
        r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (ident,))
    await r.worker.once()
    assert len(r.api.messages) == 1
    assert r.store.rows("SELECT COUNT(*) AS n FROM inventory WHERE status='DELIVERED'") == [{"n": 1}]


async def test_prolonged_free_read_outage_stays_visible_without_telegram(shop):
    r, ident = await prepare(shop, "unique_stock")
    original = r.api.call

    async def unavailable(method, **kwargs):
        if method == "get_item":
            raise APIError("HTTP_503")
        return await original(method, **kwargs)

    r.api.call = unavailable
    r.store.execute("UPDATE outbox SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (ident,))
    for _ in range(3):
        await r.worker.once()
        r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (ident,))
    issues = HomePanel(r.store, r.api).paid_issues()
    assert len(issues) == 1 and issues[0]["id"] == "waiting-paid"
    assert "автоматически" in HomePanel.paid_reason(issues[0])
    assert len(r.store.rows("SELECT * FROM inbox WHERE kind='delivery_delayed'")) == 1
    assert not r.api.messages


async def test_late_delivery_receipt_blocks_resumed_operation_without_second_send(shop):
    r, ident = await prepare(shop, "unique_stock")
    action = r.store.claim(operation_id=ident)
    r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (ident,))
    r.store.recover()
    r.store.record_late_result(
        ident, action["lease_token"], "SUCCEEDED", result={"message_id": "late-receipt"}
    )
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (ident,))
    await r.worker.once()
    assert not r.api.messages
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "UNCERTAIN"}]
    assert r.store.rows(
        "SELECT outcome FROM audit_log WHERE action='DISPATCH_OUTCOME' AND target=?", (ident,)
    ) == [{"outcome": "UNKNOWN"}]


async def test_late_receipt_arriving_during_preflight_is_checked_again_before_post(shop):
    r, ident = await prepare(shop, "unique_stock")
    old = r.store.claim(operation_id=ident)
    r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (ident,))
    r.store.recover()
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (ident,))
    original = r.api.call
    recorded = False

    async def observe(method, **kwargs):
        nonlocal recorded
        if method == "get_item" and not recorded:
            recorded = True
            r.store.record_late_result(
                ident, old["lease_token"], "SUCCEEDED", result={"message_id": "late-during-read"}
            )
        return await original(method, **kwargs)

    r.api.call = observe
    await r.worker.once()
    assert recorded and not r.api.messages
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "UNCERTAIN"}]


async def test_cancellation_before_post_resumes_same_reserved_unit_after_restart(shop):
    r, ident = await prepare(shop, "unique_stock")
    original = r.api.call
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated(method, **kwargs):
        if method == "get_item":
            entered.set()
            await release.wait()
        return await original(method, **kwargs)

    r.api.call = gated
    task = asyncio.create_task(r.worker.once())
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not r.api.messages
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "RETRYABLE_FAILED"}]
    r.api.call = original
    reopen(r)
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (ident,))
    await r.worker.once()
    assert len(r.api.messages) == 1
    assert r.store.rows("SELECT COUNT(*) AS n FROM inventory WHERE status='DELIVERED'") == [{"n": 1}]


async def test_owner_stop_during_wait_blocks_send_but_keeps_paid_obligation_visible(shop):
    r, ident = await prepare(shop, "unique_stock")
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":stop")
    await click(r, suffix=":stop_confirm")
    await r.worker.once()
    ChainWorker(r.store, r.api).once()
    assert not r.api.messages
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (ident,)) == [
        {"status": "PERMANENT_FAILED", "started_at": None}
    ]
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "CANCELLED"}]
    issues = HomePanel(r.store, r.api).paid_issues()
    assert len(issues) == 1 and issues[0]["id"] == "waiting-paid"
    assert "выключена" in HomePanel.paid_reason(issues[0])
