"""Native stock visibility: real UI, ledger and workers; only API transports controlled."""

import copy
from datetime import UTC, datetime, timedelta

import pytest

from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.notifications import Notifications
from tests.test_logical_faults import reopen
from tests.test_stage_a_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


def native_transport(r):
    r.api.item.update(
        pause_available=True,
        updated_at=datetime.now(UTC).isoformat(),
        may_be_published=False,
        republish_available=False,
        status_expiration_date=(datetime.now(UTC) + timedelta(days=2)).isoformat(),
        priority="REGULAR",
    )
    r.api.visibility_writes = []
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_item_visibility":
            return copy.deepcopy(r.api.items[kwargs["id"]])
        if method == "get_my_items":
            statuses = {getattr(value, "name", value) for value in kwargs.get("statuses", [])}
            return r.api.page(
                "items", [item for item in r.api.items.values() if not statuses or item["status"] in statuses]
            )
        return await original(method, **kwargs)

    r.api.call = call

    def discontinue_item(id):
        r.api.visibility_writes.append(("pause", id))
        r.api.items[id].update(
            status="DISCONTINUED",
            pause_available=False,
            republish_available=True,
            updated_at=datetime.now(UTC).isoformat(),
        )
        return {"accepted": True}

    def republish_item(id):
        r.api.visibility_writes.append(("resume", id))
        r.api.items[id].update(
            status="APPROVED",
            pause_available=True,
            republish_available=False,
            updated_at=datetime.now(UTC).isoformat(),
        )
        return {"accepted": True}

    r.api.account.discontinue_item = discontinue_item
    r.api.account.republish_item = republish_item


async def drain(r, count=12):
    for _ in range(count):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()


async def test_denied_native_pause_warns_once_and_never_reserves_extra_stock(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    native_transport(r)
    r.api.item["pause_available"] = False
    for i in range(4):
        Orders(r.store).ingest(r.api.order(f"denied-{i}"))
    await drain(r)
    pause = r.store.rows("SELECT id,status,started_at FROM outbox WHERE kind='stock_pause'")[0]
    assert pause["status"] == "RETRYABLE_FAILED" and pause["started_at"] is None
    await Notifications(r.store, r.bot).once()
    reopen(r)
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (pause["id"],))
    await r.worker.once(operation_id=pause["id"])
    await Notifications(r.store, r.bot).once()
    assert not r.api.visibility_writes
    assert r.api.item["status"] == "APPROVED"
    assert r.store.rows("SELECT status FROM inventory") == [{"status": "DELIVERED"}] * 3
    assert len(r.store.rows("SELECT * FROM reservations")) == 3
    assert (
        len(
            [
                call
                for call in r.session.calls
                if "Playerok не разрешил снять" in (getattr(call, "text", "") or "")
                and call.__api_method__ == "sendMessage"
            ]
        )
        == 1
    )
    assert r.store.rows(
        "SELECT status FROM notifications WHERE event_key=?", ("stock-pause-unavailable:" + pause["id"],)
    ) == [{"status": "SUCCEEDED"}]


async def test_native_empty_pause_preserves_reserved_deliveries_and_refill_resumes_same_listing(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    native_transport(r)
    expiry = r.api.item["status_expiration_date"]
    for i in range(3):
        Orders(r.store).ingest(r.api.order(f"reserved-{i}"))
    await EnrollmentWorker(r.store, r.api).once()
    await drain(r)
    assert r.api.visibility_writes == [("pause", "lot")]
    assert r.api.item["status"] == "DISCONTINUED"
    assert len(r.store.rows("SELECT 1 FROM inventory WHERE status='DELIVERED'")) == 3
    reopen(r)
    await r.ui.message("/start")
    await click(r, action="items")
    await click(r, action="itemfilter:ARCHIVE")
    await click(r, action="item:lot")
    assert "Снят с продажи" in r.session.last_panel.text
    await click(r, action="iv:delivery:lot")
    await click(r, action="stock:view:lot")
    await click(r, action="stock:add:lot")
    await r.ui.message("new-stock-after-native-pause")
    await click(r, action=next(k for k in buttons(r.session.last_panel) if k.startswith("stock:commit:")))
    await drain(r)
    assert r.api.visibility_writes == [("pause", "lot"), ("resume", "lot")]
    assert r.api.item["status"] == "APPROVED" and r.api.item["status_expiration_date"] == expiry
    Orders(r.store).ingest(r.api.order("new-sale"))
    await drain(r)
    assert [m["chat_id"] for m in r.api.messages if m["text"] == "new-stock-after-native-pause"] == [
        "chat-new-sale"
    ]
    assert not r.api.remote.payments


@pytest.mark.parametrize(
    "fault",
    [
        "stop",
        "manual-pause",
        "expired",
        "paid-republish",
        "unknown-pause",
        "unknown-resume",
        "network",
        "lease",
        "late-result",
        "foreign-readback",
    ],
)
async def test_native_visibility_guards_and_recovery(shop, fault):
    from app.playerok.adapter import APIError
    from app.services.logical_products import LogicalProducts

    r = await shop()
    await setup_ui(r, "unique_stock")
    native_transport(r)
    for i in range(3):
        Orders(r.store).ingest(r.api.order(f"guard-{i}"))
    await EnrollmentWorker(r.store, r.api).once()
    pause = r.store.rows("SELECT * FROM outbox WHERE kind='stock_pause'")[0]
    if fault == "manual-pause":
        r.api.item.update(status="DISCONTINUED", pause_available=False, republish_available=True)
    if fault == "late-result":
        r.store.record_late_result(pause["id"], "foreign-token", "UNCERTAIN", error="LATE")
    if fault == "network":
        original = r.api.call

        async def unavailable(method, **kwargs):
            if method == "get_item_visibility":
                raise APIError("NETWORK_ERROR")
            return await original(method, **kwargs)

        r.api.call = unavailable
        await r.worker.once(operation_id=pause["id"])
        assert r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (pause["id"],)) == [
            {"status": "RETRYABLE_FAILED", "started_at": None}
        ]
        r.api.call = original
        r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (pause["id"],))
    if fault == "lease":
        assert r.store.claim(operation_id=pause["id"])
        r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (pause["id"],))
        r.store.recover()
        assert r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (pause["id"],)) == [
            {"status": "RETRYABLE_FAILED", "started_at": None}
        ]
        r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (pause["id"],))
    if fault == "unknown-pause":
        original = r.api.account.discontinue_item

        def lost(id):
            original(id)
            raise TimeoutError()

        r.api.account.discontinue_item = lost
    if fault == "foreign-readback":
        original = r.api.call

        async def wrong(method, **kwargs):
            result = await original(method, **kwargs)
            if method == "get_item_visibility" and r.api.visibility_writes:
                result["user"] = {"id": "foreign"}
            return result

        r.api.call = wrong
    await r.worker.once(operation_id=pause["id"])
    if fault in {"late-result", "unknown-pause", "foreign-readback"}:
        assert r.store.rows("SELECT status FROM outbox WHERE id=?", (pause["id"],)) == [
            {"status": "UNCERTAIN"}
        ]
        writes = copy.deepcopy(r.api.visibility_writes)
        reopen(r)
        await drain(r, 4)
        assert r.api.visibility_writes == writes
        return
    if fault in {"network", "lease"}:
        assert r.api.visibility_writes == [("pause", "lot")]
        return
    await r.ui.click("stock:add:lot")
    await r.ui.message("guard-new-unit")
    await click(r, action=next(k for k in buttons(r.session.last_panel) if k.startswith("stock:commit:")))
    if fault == "expired":
        r.api.item["status_expiration_date"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    if fault == "paid-republish":
        r.api.item["may_be_published"] = True
    if fault == "stop":
        logical = LogicalProducts(r.store, r.api)
        logical.stop(7, logical.listing("lot")["id"])
    if fault == "unknown-resume":
        original = r.api.account.republish_item

        def lost(id):
            original(id)
            raise TimeoutError()

        r.api.account.republish_item = lost
    await drain(r, 4)
    if fault == "manual-pause":
        assert not r.api.visibility_writes
    elif fault == "unknown-resume":
        assert r.api.visibility_writes == [("pause", "lot"), ("resume", "lot")]
        assert r.store.rows("SELECT status FROM outbox WHERE kind='stock_resume'") == [
            {"status": "UNCERTAIN"}
        ]
        reopen(r)
        await drain(r, 4)
        assert len(r.api.visibility_writes) == 2
    else:
        assert r.api.visibility_writes == [("pause", "lot")]
    assert not r.api.remote.payments


async def test_waiting_paid_order_is_not_permanently_blocked_while_native_resume_is_queued(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    native_transport(r)
    for i in range(4):
        Orders(r.store).ingest(r.api.order(f"waiting-{i}"))
    await drain(r)
    assert r.api.item["status"] == "DISCONTINUED"
    await r.ui.click("stock:add:lot")
    await r.ui.message("for-existing-paid-waiter")
    await click(r, action=next(k for k in buttons(r.session.last_panel) if k.startswith("stock:commit:")))
    r.store.execute("UPDATE auto_enrollment SET due_at=0")
    await drain(r)
    assert [m["chat_id"] for m in r.api.messages if m["text"] == "for-existing-paid-waiter"] == [
        "chat-waiting-3"
    ]
    assert r.api.visibility_writes == [("pause", "lot"), ("resume", "lot"), ("pause", "lot")]
    assert not r.api.remote.payments


async def test_cancel_during_read_before_native_post_is_retryable_without_manual_recovery(shop):
    import asyncio

    r = await shop()
    await setup_ui(r, "unique_stock")
    native_transport(r)
    for i in range(3):
        Orders(r.store).ingest(r.api.order(f"cancel-{i}"))
    await EnrollmentWorker(r.store, r.api).once()
    ident = r.store.rows("SELECT id FROM outbox WHERE kind='stock_pause'")[0]["id"]
    entered = asyncio.Event()
    hold = asyncio.Event()
    original = r.api.call

    async def waiting(method, **kwargs):
        if method == "get_item_visibility":
            entered.set()
            await hold.wait()
        return await original(method, **kwargs)

    r.api.call = waiting
    task = asyncio.create_task(r.worker.once(operation_id=ident))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (ident,)) == [
        {"status": "RETRYABLE_FAILED", "started_at": None}
    ]
    assert not r.api.visibility_writes


async def test_new_manual_marketplace_pause_is_not_overridden_by_old_bot_pause_receipt(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    native_transport(r)
    for i in range(3):
        Orders(r.store).ingest(r.api.order(f"other-pause-{i}"))
    await drain(r)
    # An owner has independently resumed and paused the listing again.
    r.api.item["updated_at"] = datetime.now(UTC).isoformat()
    await r.ui.click("stock:add:lot")
    await r.ui.message("stock-after-manual-pause")
    await click(r, action=next(k for k in buttons(r.session.last_panel) if k.startswith("stock:commit:")))
    await drain(r, 4)
    assert r.api.visibility_writes == [("pause", "lot")]
    assert r.api.item["status"] == "DISCONTINUED"


async def test_manually_resumed_same_listing_is_confirmed_without_second_native_request(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    native_transport(r)
    for i in range(3):
        Orders(r.store).ingest(r.api.order(f"outside-resume-{i}"))
    await drain(r)
    r.api.item.update(
        status="APPROVED",
        pause_available=True,
        republish_available=False,
        updated_at=datetime.now(UTC).isoformat(),
    )
    await r.ui.click("stock:add:lot")
    await r.ui.message("after-owner-resumed")
    await click(r, action=next(k for k in buttons(r.session.last_panel) if k.startswith("stock:commit:")))
    await drain(r, 4)
    assert r.api.visibility_writes == [("pause", "lot")]
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE kind='stock_resume'") == [
        {"status": "SUCCEEDED", "started_at": None}
    ]
    assert not r.api.remote.payments


async def test_unknown_native_pause_has_read_only_existing_reconciliation_without_success_claim(shop):
    from app.services.reconciliation import Reconciliation

    r = await shop()
    await setup_ui(r, "unique_stock")
    native_transport(r)
    for i in range(3):
        Orders(r.store).ingest(r.api.order(f"read-only-{i}"))
    await EnrollmentWorker(r.store, r.api).once()
    ident = r.store.rows("SELECT id FROM outbox WHERE kind='stock_pause'")[0]["id"]
    original = r.api.account.discontinue_item

    def lost(id):
        original(id)
        raise TimeoutError()

    r.api.account.discontinue_item = lost
    await r.worker.once(operation_id=ident)
    evidence = await Reconciliation(r.store, r.api).evidence(7, ident)
    assert evidence["decision"] == "CURRENT_STATE_ONLY" and "снят с продажи" in evidence["reason"]
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "UNCERTAIN"}]
    assert r.api.visibility_writes == [("pause", "lot")]


async def test_safe_marketing_change_after_pause_enqueue_does_not_destroy_native_recovery(shop):
    from app.workers.logical_products import LogicalProductWorker

    r = await shop()
    await setup_ui(r, "unique_stock")
    native_transport(r)
    for i in range(3):
        Orders(r.store).ingest(r.api.order(f"marketing-{i}"))
    await EnrollmentWorker(r.store, r.api).once()
    r.api.item["name"] = "Updated " + r.api.item["name"]
    await r.worker.once()
    await LogicalProductWorker(r.store, r.api).once()
    await drain(r, 4)
    assert r.api.visibility_writes == [("pause", "lot")]
