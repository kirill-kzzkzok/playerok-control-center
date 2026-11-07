"""Restocking is data input, not a second permission to resume previously armed sales."""

import pytest

from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_logical_faults import reopen
from tests.test_stage_a_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("owner_stop", [False, True])
async def test_paid_stock_waiter_continues_after_upload_without_resume_but_never_overrides_stop(
    shop, owner_stop, restart
):
    r = await shop()
    await setup_ui(r, "unique_stock")
    for index in range(4):
        Orders(r.store).ingest(r.api.order(f"stock-{index}"))
    await EnrollmentWorker(r.store, r.api).once()
    for _ in range(12):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert len(r.store.rows("SELECT 1 FROM inventory WHERE status='DELIVERED'")) == 3
    if owner_stop:
        await r.ui.click("lp:item:lot")
        await click(r, suffix=":stop")
        await click(r, suffix=":stop_confirm")
    await r.ui.click("stock:add:lot")
    await r.ui.message("unique-after-top-up")
    await click(r, action=next(k for k in buttons(r.session.last_panel) if k.startswith("stock:commit:")))
    if restart:
        reopen(r)
    r.store.execute("UPDATE auto_enrollment SET due_at=0")  # Advance the existing retry deadline.
    for _ in range(8):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert len(r.store.rows("SELECT 1 FROM inventory WHERE status='DELIVERED'")) == (3 if owner_stop else 4)
    assert [m["chat_id"] for m in r.api.messages if m["text"] == "unique-after-top-up"] == (
        [] if owner_stop else ["chat-stock-3"]
    )
    assert not r.api.remote.payments


async def test_one_remaining_unit_notifies_once_and_zero_blocks_new_admission(shop):
    from app.workers.notifications import Notifications

    r = await shop()
    await setup_ui(r, "unique_stock")
    r.store.set_setting("notifications:initialized", True)
    for index in range(2):
        Orders(r.store).ingest(r.api.order(f"threshold-{index}"))
    await EnrollmentWorker(r.store, r.api).once()
    await Notifications(r.store, r.bot).once()
    notices = r.store.rows(
        "SELECT n.status FROM notifications n JOIN inbox i ON i.key=n.event_key WHERE i.kind='stock_low'"
    )
    assert notices == [{"status": "SUCCEEDED"}]
    assert len(r.store.rows("SELECT 1 FROM inventory WHERE status='AVAILABLE'")) == 1
    await EnrollmentWorker(r.store, r.api).once()
    await Notifications(r.store, r.bot).once()
    assert (
        r.store.rows(
            "SELECT n.status FROM notifications n JOIN inbox i ON i.key=n.event_key WHERE i.kind='stock_low'"
        )
        == notices
    )
    for index in (2, 3):
        Orders(r.store).ingest(r.api.order(f"threshold-{index}"))
    await EnrollmentWorker(r.store, r.api).once()
    await Notifications(r.store, r.bot).once()
    assert r.store.rows("SELECT status,error_code FROM auto_enrollment WHERE order_id='threshold-3'") == [
        {"status": "BLOCKED", "error_code": "STOCK_EMPTY"}
    ]
    assert len(r.store.rows("SELECT * FROM reservations")) == 3
    assert not r.api.messages and not r.api.remote.payments


async def test_restock_supersedes_pending_low_notice_instead_of_sending_two(shop):
    from app.workers.notifications import Notifications

    r = await shop()
    await setup_ui(r, "unique_stock")
    r.store.set_setting("notifications:initialized", True)
    for index in range(3):
        Orders(r.store).ingest(r.api.order(f"old-stock-{index}"))
    await EnrollmentWorker(r.store, r.api).once()
    await r.ui.click("stock:add:lot")
    await r.ui.message("only-new-unit")
    await click(r, action=next(k for k in buttons(r.session.last_panel) if k.startswith("stock:commit:")))
    await Notifications(r.store, r.bot).once()
    statuses = r.store.rows(
        "SELECT n.status FROM notifications n JOIN inbox i ON i.key=n.event_key WHERE i.kind='stock_low' ORDER BY i.rowid"
    )
    assert statuses == [{"status": "SKIPPED"}, {"status": "SUCCEEDED"}]
    assert not r.api.messages and not r.api.remote.payments
