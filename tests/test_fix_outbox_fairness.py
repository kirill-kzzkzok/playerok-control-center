"""Actual paid workers must progress beside a durable backlog of old messages."""

from datetime import UTC, datetime, timedelta

import pytest

from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_logical_faults import reopen
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize("handler", ["fixed_text", "unique_stock"])
async def test_ten_paid_deliveries_start_before_old_backlog_and_both_queues_drain(shop, handler):
    r = await shop()
    await setup_ui(r, handler, "paid-content" if handler == "fixed_text" else None)
    if handler == "unique_stock":
        await r.ui.click("stock:add:lot")
        await r.ui.message("\n".join(f"extra-{i}" for i in range(10)))
        await click(r, action=next(k for k in buttons(r.session.last_panel) if k.startswith("stock:commit:")))
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    for i in range(100):
        ident = r.store.enqueue(f"old-reply-{i}", "reply", f"old-chat-{i}", {"text": f"old-{i}"})
        r.store.execute("UPDATE outbox SET created_at=? WHERE id=?", (old, ident))
    for i in range(10):
        Orders(r.store).ingest(r.api.order(f"paid-{i}"))
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    reopen(r)
    first = None
    old_during_paid = False
    for turn in range(180):
        ChainWorker(r.store, r.api).once()
        await r.worker.once(excluded_kinds=("gift_send", "premium_publish", "premium_bump"))
        paid = [m for m in r.api.messages if m["chat_id"].startswith("chat-paid-")]
        ordinary = [m for m in r.api.messages if m["chat_id"].startswith("old-chat-")]
        if paid and first is None:
            first = turn
        if ordinary and len(paid) < 10:
            old_during_paid = True
    assert first is not None and first <= 4, first
    assert old_during_paid
    assert len([m for m in r.api.messages if m["chat_id"].startswith("old-chat-")]) == 100
    for i in range(10):
        deliveries = [
            m
            for m in r.api.messages
            if m["chat_id"] == f"chat-paid-{i}" and m["text"] != "Спасибо за покупку!"
        ]
        assert len(deliveries) == 1
    assert not r.store.rows("SELECT id FROM outbox WHERE status!='SUCCEEDED'")
