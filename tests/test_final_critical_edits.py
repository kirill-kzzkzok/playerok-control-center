"""Critical edits cannot silently disable delivery or invalidate paid obligations."""

import asyncio

import pytest

from app.gifts.worker import GiftWorker
from app.playerok.adapter import APIError
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.workers.enrollment import EnrollmentWorker
from tests.test_telegram_transport import production_dispatcher
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_unified_delivery_controls import state, stop
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_active_delivery_requires_explicit_stop_before_critical_input(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    before = r.store.rows("SELECT * FROM logical_products")
    await r.ui.click("editfields:lot")
    assert "Сначала остановите" in r.session.last_panel.text
    assert "lp:item:lot" in buttons(r.session.last_panel)
    assert r.store.rows("SELECT * FROM logical_products") == before
    assert not (await state(r).get_data()).get("edit_target")


@pytest.mark.parametrize("phase", ["paid", "reserved"])
async def test_critical_edit_after_stop_preserves_existing_paid_obligations(shop, phase):
    r = await shop()
    await setup_ui(r, "unique_stock")
    Orders(r.store).ingest(r.api.order("before-edit"))
    if phase == "reserved":
        await EnrollmentWorker(r.store, r.api).once()
    await stop(r)
    inventory = r.store.rows("SELECT * FROM inventory")
    reservations = r.store.rows("SELECT * FROM reservations")
    await r.ui.click("editdesc:lot")
    assert "незавершён" in r.session.last_panel.text
    assert "order:before-edit" in buttons(r.session.last_panel)
    assert r.store.rows("SELECT * FROM inventory") == inventory
    assert r.store.rows("SELECT * FROM reservations") == reservations


async def queue_description(r):
    await r.ui.click("editdesc:lot")
    await r.ui.message("Новое подтверждаемое описание товара")
    data = await state(r).get_data()
    await r.ui.click("commit:" + data["confirm_token"])


async def test_active_gift_can_edit_marketing_description_without_rearming(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    logical = LogicalProducts(r.store, r.api)
    before = logical.listing("lot")
    await queue_description(r)
    await r.worker.once()
    assert r.api.item["description"] == "Новое подтверждаемое описание товара"
    after = logical.listing("lot")
    for key in ("id", "code", "revision", "control_revision", "armed_at", "enabled", "config"):
        assert after[key] == before[key]
    assert after["verified_at"]
    assert r.store.rows("SELECT enabled FROM gift_listings") == [{"enabled": 1}]
    edit = r.store.rows("SELECT id FROM outbox WHERE kind='edit' ORDER BY rowid")[-1]["id"]
    await r.ui.click("result:show:" + edit)
    assert "Выдача остаётся остановленной" not in r.session.last_panel.text

    Orders(r.store).ingest(r.api.order("paid-after-description"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    assert len(r.api.remote.payments) == 1


async def test_gift_waits_for_in_progress_description_readback(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    await queue_description(r)
    edit = r.store.rows("SELECT id FROM outbox WHERE kind='edit' ORDER BY rowid")[-1]["id"]
    reading, resume = asyncio.Event(), asyncio.Event()
    original = r.api.call

    async def call(method, **kwargs):
        if (
            asyncio.current_task().get_name() == "edit-readback"
            and method == "get_item"
            and r.api.item["description"] == "Новое подтверждаемое описание товара"
        ):
            reading.set()
            await resume.wait()
        return await original(method, **kwargs)

    r.api.call = call
    worker = asyncio.create_task(r.worker.once(operation_id=edit), name="edit-readback")
    try:
        await asyncio.wait_for(reading.wait(), 5)
        Orders(r.store).ingest(r.api.order("paid-during-edit"))
        assert await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once() == 1
        gift = r.store.rows("SELECT id FROM outbox WHERE kind='gift_send'")[0]["id"]
        await r.worker.once(operation_id=gift)
        assert r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (gift,)) == [
            {"status": "RETRYABLE_FAILED", "started_at": None}
        ]
        assert not r.api.remote.payments
    finally:
        resume.set()
        await worker
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (gift,))
    await r.worker.once(operation_id=gift)
    assert len(r.api.remote.payments) == 1


@pytest.mark.parametrize(
    "fault", ["external_description", "external_fields", "sibling_response", "lost_reply"]
)
async def test_active_description_edit_keeps_preflight_and_unknown_fences(shop, fault):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    logical = LogicalProducts(r.store, r.api)
    prior = logical.listing("lot")
    await queue_description(r)
    if fault == "external_description":
        r.api.item["description"] = "Другой текст с сайта"
    elif fault == "external_fields":
        r.api.item["attributes"] = {"recipient": "changed"}
    elif fault == "sibling_response":
        # An old verified sibling is legitimate history, but the API response
        # must not substitute it for the item named by this queued edit.
        r.store.execute(
            "INSERT INTO logical_listings(item_id,product_id,config_revision,description_sha256,"
            "server_status,verified_at,title_snapshot) SELECT 'sibling',product_id,config_revision,"
            "description_sha256,server_status,verified_at,title_snapshot FROM logical_listings WHERE item_id='lot'"
        )
        original = r.api.call

        async def call(method, **kwargs):
            result = await original(method, **kwargs)
            return {**result, "id": "sibling"} if method == "get_item" else result

        r.api.call = call
    else:
        update = r.api.account.update_item

        def lost(**kwargs):
            update(**kwargs)
            raise APIError("NETWORK_ERROR", uncertain=True)

        r.api.account.update_item = lost
    before = len(r.api.mutations)
    await r.worker.once()
    result = r.store.rows("SELECT status,started_at FROM outbox WHERE kind='edit' ORDER BY rowid")[-1]
    assert result["status"] == ("UNCERTAIN" if fault == "lost_reply" else "PERMANENT_FAILED")
    assert bool(result["started_at"]) == (fault == "lost_reply")
    assert not await r.worker.once()
    assert len(r.api.mutations) == before + (fault == "lost_reply")
    assert logical.listing("lot") == prior
    assert not r.api.remote.payments


async def test_payment_after_edit_queue_prevents_mutation_without_destroying_reserve(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    await stop(r)
    await queue_description(r)
    Orders(r.store).ingest(r.api.order("arrived-after-preview"))
    before = len(r.api.mutations)
    await r.worker.once()
    assert len(r.api.mutations) == before
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE kind='edit' ORDER BY created_at")[-1] == {
        "status": "PERMANENT_FAILED",
        "started_at": None,
    }


async def test_queued_critical_edit_blocks_rearm_then_requires_fresh_binding(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    await stop(r)
    await queue_description(r)
    logical = LogicalProducts(r.store, r.api)
    product = logical.listing("lot")
    with pytest.raises(ValueError, match="[Рр]едактирован"):
        await logical.arm(7, product["id"], product["revision"], item_id="lot")
    await r.worker.once()
    assert logical.listing("lot")["verified_at"] is None
    assert not logical.listing("lot")["enabled"]
    assert r.store.rows("SELECT status FROM outbox WHERE kind='edit' ORDER BY created_at")[-1] == {
        "status": "SUCCEEDED"
    }
    assert not r.api.messages
    inventory = r.store.rows("SELECT * FROM inventory")
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":rebind")
    await click(r, suffix=":commit")
    await r.worker.once()
    confirmed = logical.listing("lot")
    assert confirmed["verified_at"] and confirmed["id"] == product["id"]
    assert confirmed["code"] == product["code"]
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":arm")
    await click(r, suffix=":enable:inherit")
    assert logical.listing("lot")["enabled"]
    assert r.store.rows("SELECT * FROM inventory") == inventory
    assert not r.api.messages


async def test_edit_queued_during_arm_network_read_wins_over_late_enable(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    await stop(r)
    logical = LogicalProducts(r.store, r.api)
    product = logical.listing("lot")
    original = r.api.call
    reads = 0

    async def call(method, **kwargs):
        nonlocal reads
        result = await original(method, **kwargs)
        if method == "get_item":
            reads += 1
            if reads == 2:
                await queue_description(r)
        return result

    r.api.call = call
    with pytest.raises(ValueError, match="[Рр]едактирован"):
        await logical.arm(7, product["id"], product["revision"], item_id="lot")
    assert reads >= 2
    assert not logical.listing("lot")["enabled"]
    assert not r.store.rows("SELECT 1 FROM delivery_policies WHERE json_extract(payload,'$.enabled')=1")
    assert not r.api.messages


async def test_lost_critical_edit_reply_never_repeats_or_enables_delivery(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    await stop(r)
    await queue_description(r)
    update = r.api.account.update_item

    def lost(**kwargs):
        update(**kwargs)
        raise APIError("NETWORK_ERROR", uncertain=True)

    r.api.account.update_item = lost
    before = len(r.api.mutations)
    await r.worker.once()
    assert len(r.api.mutations) == before + 1
    assert r.store.rows("SELECT status FROM outbox WHERE kind='edit' ORDER BY created_at")[-1] == {
        "status": "UNCERTAIN"
    }
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.click("editdesc:lot")
    assert "ops:list:ALL:0" in buttons(r.session.last_panel) or "lp:item:lot" in buttons(r.session.last_panel)
    assert not await r.worker.once()
    assert len(r.api.mutations) == before + 1
    assert not LogicalProducts(r.store, r.api).listing("lot")["enabled"]
