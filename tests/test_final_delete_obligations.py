"""Deletion protects actual paid obligations, including legacy callbacks and late sales."""

import pytest

from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_stability_deal_completeness import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_ux_redesign import buttons


@pytest.fixture
async def shop(tmp_path):
    async for create in shop_fixture.__wrapped__(tmp_path):
        yield create


@pytest.mark.parametrize("phase", ["paid", "reserved", "queued", "unknown"])
async def test_product_cannot_be_deleted_with_paid_obligation(shop, phase):
    r = await shop()
    await setup_ui(r, "unique_stock")
    Orders(r.store).ingest(r.api.order("uncompleted"))
    if phase != "paid":
        await EnrollmentWorker(r.store, r.api).once()
    if phase in {"queued", "unknown"}:
        ChainWorker(r.store, r.api).once()
    if phase == "unknown":
        original = r.api.account.send_message

        def lose_ack(**kwargs):
            original(**kwargs)
            raise APIError("NETWORK_ERROR", uncertain=True)

        r.api.account.send_message = lose_ack
        await r.worker.once()
    await r.ui.click("iv:other:lot")
    assert "delete:lot" not in buttons(r.session.last_panel)
    await r.ui.click("delete:lot")  # saved pre-upgrade button
    assert not any(x.startswith("commit:") for x in buttons(r.session.last_panel))
    assert "order:uncompleted" in buttons(r.session.last_panel)
    assert not r.store.rows("SELECT * FROM outbox WHERE kind='delete'")


async def test_paid_order_arriving_after_delete_preview_blocks_worker(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    removed = []
    r.api.account.remove_item = lambda **kwargs: removed.append(kwargs) or True
    await r.ui.click("delete:lot")
    await click(
        r, suffix=None, action=next(x for x in buttons(r.session.last_panel) if x.startswith("commit:"))
    )
    Orders(r.store).ingest(r.api.order("new-paid"))
    await r.worker.once()
    assert not removed
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE kind='delete'") == [
        {"status": "PERMANENT_FAILED", "started_at": None}
    ]


async def confirm_delete(r, item="lot"):
    await r.ui.click("delete:" + item)
    token = next(x for x in buttons(r.session.last_panel) if x.startswith("commit:"))
    await r.ui.click(token)


async def test_order_arriving_during_owned_item_read_still_blocks_delete_dispatch(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    removed = []
    r.api.account.remove_item = lambda **kwargs: removed.append(kwargs) or True
    await confirm_delete(r)
    original = r.api.call

    async def call(method, **kwargs):
        value = await original(method, **kwargs)
        if method == "get_item":
            Orders(r.store).ingest(r.api.order("during-read"))
        return value

    r.api.call = call
    await r.worker.once()
    assert not removed
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE kind='delete'") == [
        {"status": "PERMANENT_FAILED", "started_at": None}
    ]


@pytest.mark.parametrize("completed_sale", ["draft", "complete", "reminder"])
async def test_safe_deletion_preserves_confirmed_history_and_inventory(shop, completed_sale):
    r = await shop()
    if completed_sale != "draft":
        await setup_ui(r, "unique_stock", completion=completed_sale == "reminder")
        Orders(r.store).ingest(r.api.order("done"))
        await EnrollmentWorker(r.store, r.api).once()
        for _ in range(5):
            ChainWorker(r.store, r.api).once()
            await r.worker.once()
        assert r.store.rows("SELECT status FROM delivery_chains") == [
            {"status": "NEEDS_FULFILLMENT" if completed_sale == "reminder" else "SUCCEEDED"}
        ]
        r.api.orders["done"]["status"] = "CONFIRMED"
        Orders(r.store).ingest(r.api.orders["done"])
    else:
        r.api.item["status"] = "DRAFT"
    inventory = r.store.rows("SELECT * FROM inventory")
    reservations = r.store.rows("SELECT * FROM reservations")
    removed = []
    r.api.account.remove_item = lambda **kwargs: removed.append(kwargs) or True
    await confirm_delete(r)
    await r.worker.once()
    assert removed == [{"id": "lot"}]
    assert r.store.rows("SELECT status FROM outbox WHERE kind='delete'") == [{"status": "SUCCEEDED"}]
    assert r.store.rows("SELECT * FROM inventory") == inventory
    assert r.store.rows("SELECT * FROM reservations") == reservations
    if completed_sale != "draft":
        assert r.store.rows("SELECT status FROM orders") == [{"status": "CONFIRMED"}]


async def test_unknown_delete_result_never_offers_another_delete_confirmation(shop):
    r = await shop()
    r.api.item["status"] = "DRAFT"
    removed = []

    def remove(**kwargs):
        removed.append(kwargs)
        raise APIError("NETWORK_ERROR", uncertain=True)

    r.api.account.remove_item = remove
    await confirm_delete(r)
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE kind='delete'") == [{"status": "UNCERTAIN"}]
    await r.ui.click("delete:lot")
    assert not any(x.startswith("commit:") for x in buttons(r.session.last_panel))
    assert "ops:list:ALL:0" in buttons(r.session.last_panel)
    assert not await r.worker.once()
    assert len(removed) == 1


async def test_new_alias_deletion_is_blocked_by_original_alias_paid_obligation(shop):
    from copy import deepcopy

    from app.services.logical_products import LogicalProducts
    from app.workers.logical_products import LogicalProductWorker

    r = await shop()
    await setup_ui(r, "unique_stock")
    r.api.item["status"] = "SOLD"
    Orders(r.store).ingest(r.api.order("original-paid"))
    r.api.items["next"] = {**deepcopy(r.api.item), "id": "next", "status": "APPROVED"}
    await LogicalProductWorker(r.store, r.api).once()
    assert LogicalProducts(r.store, r.api).listing("next")
    await r.ui.click("delete:next")
    assert "order:original-paid" in buttons(r.session.last_panel)
    assert not any(x.startswith("commit:") for x in buttons(r.session.last_panel))


async def test_late_delete_receipt_after_lease_recovery_does_not_authorize_replay(shop):
    import asyncio
    import threading

    r = await shop()
    r.api.item["status"] = "DRAFT"
    entered, release = threading.Event(), threading.Event()
    removed = []

    def remove(**kwargs):
        removed.append(kwargs)
        entered.set()
        assert release.wait(5)
        return True

    r.api.account.remove_item = remove
    await confirm_delete(r)
    task = asyncio.create_task(r.worker.once())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        op = r.store.rows("SELECT * FROM outbox WHERE kind='delete'")[0]
        assert op["started_at"]
        r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (op["id"],))
        assert r.store.recover() == 1
    finally:
        release.set()
        await task
    assert r.store.rows("SELECT status FROM outbox WHERE kind='delete'") == [{"status": "UNCERTAIN"}]
    assert r.store.rows("SELECT observed_status FROM outbox_late_results") == [
        {"observed_status": "SUCCEEDED"}
    ]
    assert not await r.worker.once()
    assert len(removed) == 1


async def test_unknown_stars_payment_blocks_delete_even_after_deal_is_confirmed(shop):
    from app.gifts.worker import GiftWorker

    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    order = r.api.order("closed-but-stars-unknown")
    Orders(r.store).ingest(order)
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    r.api.remote.pay_error = TimeoutError()
    await r.worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "UNCERTAIN"}]
    order["status"] = "CONFIRMED"
    Orders(r.store).ingest(order)
    await r.ui.click("delete:lot")
    assert "Подарочная операция" in r.session.last_panel.text
    assert not any(x.startswith("commit:") for x in buttons(r.session.last_panel))
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind='delete'")
    assert len(r.api.remote.payments) == 1


async def test_blocked_delete_screen_invalidates_previous_delete_confirmation(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    await r.ui.click("delete:lot")
    old = next(x for x in buttons(r.session.last_panel) if x.startswith("commit:"))
    Orders(r.store).ingest(r.api.order("new-paid-before-confirm"))
    await r.ui.click("delete:lot")
    assert "order:new-paid-before-confirm" in buttons(r.session.last_panel)
    await r.ui.click(old)
    assert not r.store.rows("SELECT * FROM outbox WHERE kind='delete'")
