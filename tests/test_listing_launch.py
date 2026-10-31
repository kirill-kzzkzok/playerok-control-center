"""Production launch, delivery and Dispatcher; no live HTTP or paid Telegram RPCs."""

import json
from copy import deepcopy

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, now
from app.gifts.worker import GiftWorker
from app.playerok.adapter import APIError
from app.services.listing_launch import ListingLaunch
from app.services.orders import Orders
from app.services.products import ValidationError
from app.telegram.storage import SQLiteStorage
from tests.test_gifts import rig  # noqa: F401 -- shared temporary SQLite and real TL test transport
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


@pytest.fixture
def launch(rig):
    r = rig
    r.api.item.update(status="DRAFT", price=100, raw_price=100)
    r.store.set_setting("sync:health", {"status": "OK"})
    original = r.api.call
    r.quotes = [
        {"id": "free", "type": "DEFAULT", "price": 0, "period": 7},
        {"id": "premium", "type": "PREMIUM", "price": 10, "period": 7},
    ]
    r.publications = []
    r.failure = None

    async def call(method, **kwargs):
        if method == "get_item_priority_statuses":
            return deepcopy(r.quotes)
        if method == "get":
            return {"id": "shop", "profile": {"balance": {"available": 100}}}
        if method == "get_my_items":
            return {"items": [deepcopy(r.api.item)], "page_info": {}}
        return await original(method, **kwargs)

    def publish(**kwargs):
        binding = r.service.listing("item")
        policy = r.store.rows("SELECT * FROM delivery_policies WHERE item_id='item'")
        r.publications.append({"binding": deepcopy(binding), "policy": deepcopy(policy)})
        if r.failure == "before":
            raise APIError("HTTP_400")
        r.api.item["status"] = "APPROVED"
        if r.premium:
            r.api.item["priority"] = "PREMIUM"
            r.api.item["status_payment"] = {
                "id": "receipt-launch",
                "status": "CONFIRMED",
                "direction": "OUT",
                "operation": "ITEM_PREMIUM_PRIORITY",
                "provider_id": "LOCAL",
                "value": 10,
            }
        Orders(r.store).ingest(r.api.add_order("immediate"))
        if r.failure == "lost_ack":
            raise APIError("NETWORK_ERROR", uncertain=True)
        return deepcopy(r.api.item)

    r.premium = False
    r.api.call = call
    r.api.account.publish_item = publish
    r.launch = ListingLaunch(r.store, r.api, r.service)
    return r


async def configured(r, mode="gifts", **options):
    r.premium = options.get("publication") == "PREMIUM"
    ident = await r.launch.open(
        7,
        "item",
        {"delivery": mode, "delivery_data": "secret-one\nsecret-two" if mode == "stock" else "secret-text"},
    )
    draft = r.launch.load(7, ident)
    value = draft["payload"]
    value.update(options)
    value["ready"] = await r.launch.check(7, value)
    r.store.update_draft(ident, 7, draft["revision"], value)
    return ident, r.launch.load(7, ident)["revision"]


async def test_gifts_armed_before_publication_and_first_immediate_sale_is_enrolled(launch):
    r = launch
    ident, revision = await configured(r, auto_complete=True, after="Thanks")
    operation = await r.launch.queue(7, ident, revision)
    assert r.api.item["status"] == "DRAFT"
    armed = r.service.listing("item")
    assert armed["enabled"] and armed["auto_fulfill"]
    assert not r.remote.payments
    assert await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (operation,))[0]["status"] == "SUCCEEDED"
    assert r.publications[0]["binding"]["armed_at"] == armed["armed_at"]
    assert r.service.discover() == 1
    assert await r.worker.once()
    assert len(r.remote.payments) == 1
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    await r.worker.once()
    assert r.api.orders["immediate"]["status"] == "SENT"
    assert r.api.sent[0][1] == "Thanks"


@pytest.mark.parametrize("mode", ["text", "stock"])
async def test_prepared_text_and_stock_are_enabled_before_publication(launch, mode):
    r = launch
    ident, revision = await configured(r, mode)
    op = await r.launch.queue(7, ident, revision)
    assert not r.service.listing("item")
    assert await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "SUCCEEDED"
    saved = r.publications[0]["policy"][0]
    assert json.loads(saved["payload"])["auto_enroll"] is True
    assert json.loads(saved["payload"])["enabled"] is True
    assert saved["updated_at"] <= now()
    if mode == "stock":
        assert len(r.store.rows("SELECT * FROM inventory WHERE item_id='item' AND status='AVAILABLE'")) == 2


async def test_restart_between_preparation_and_publish_no_second_queue_or_late_arm(launch):
    r = launch
    ident, revision = await configured(r)
    await r.launch.queue(7, ident, revision)
    armed = r.service.listing("item")["armed_at"]
    resumed = ListingLaunch(r.store, r.api, r.service)
    with pytest.raises(Conflict):
        await resumed.queue(7, ident, revision)
    await r.worker.once()
    assert len(r.publications) == 1 and r.service.listing("item")["armed_at"] == armed


@pytest.mark.parametrize("change", ["price", "account", "description", "gift_price", "config"])
async def test_changed_authority_or_price_invalidates_confirmation(launch, change):
    r = launch
    ident, revision = await configured(r)
    if change == "price":
        r.api.item["price"] += 1
    elif change == "account":
        r.api.account.id = "other"
    elif change == "description":
        r.api.item["description"] += "\nchanged"
    elif change == "gift_price":
        r.remote.gift.stars += 1
    else:
        draft = r.launch.load(7, ident)
        draft["payload"]["after"] = "changed"
        r.store.update_draft(ident, 7, revision, draft["payload"])
    with pytest.raises((ValueError, Conflict)):
        await r.launch.queue(7, ident, revision)
    assert not r.publications and not r.remote.payments


@pytest.mark.parametrize("failure", ["before", "lost_ack"])
async def test_publication_failure_and_unknown_never_republish(launch, failure):
    r = launch
    ident, revision = await configured(r)
    r.failure = failure
    op = await r.launch.queue(7, ident, revision)
    await r.worker.once()
    status = r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"]
    assert status == ("UNCERTAIN" if failure == "lost_ack" else "PERMANENT_FAILED")
    with pytest.raises(Conflict):
        await r.launch.queue(7, ident, revision)
    await r.worker.once()
    assert len(r.publications) == 1
    if failure == "lost_ack":
        assert r.api.item["status"] == "APPROVED" and r.service.listing("item")["enabled"]
        assert r.service.discover() == 1
    else:
        assert r.api.item["status"] == "DRAFT"


async def test_stop_after_queue_blocks_publication(launch):
    r = launch
    ident, revision = await configured(r)
    await r.launch.queue(7, ident, revision)
    r.service.stop(7)
    await r.worker.once()
    assert not r.publications and not r.remote.payments


async def test_failed_preparation_releases_launch_marker_for_plain_publish(launch):
    r = launch
    # A running text auto-delivery on the same item makes manual-mode queue() fail
    # exactly at the stop-check, after launch:prepared was already recorded.
    ident, revision = await configured(r, mode="manual")
    # An enabled delivery policy on the same item makes queue() fail at the
    # stop-check, after launch:prepared was already recorded.
    r.store.execute(
        'INSERT INTO delivery_policies VALUES(\'item\',1,7,\'{"enabled": true, "mode": "text",'
        ' "text": "x", "greeting": "", "after": "", "mark_sent": false, "auto_enroll": true}\','
        " '2026-10-31T00:00:00+00:00')"
    )
    with pytest.raises(Conflict):
        await r.launch.queue(7, ident, revision)
    assert r.store.get_draft(ident, 7)["state"] == "DRAFT"
    assert not r.store.setting("launch:prepared:item")
    op = r.store.enqueue(
        "plain:publish2", "publish", "item", {"priority_id": "free", "price": 100, "raw_price": 100}
    )
    await r.worker.once()
    row = r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]
    assert row["status"] == "SUCCEEDED", row
    assert r.publications and r.api.item["status"] == "APPROVED"


async def test_permanent_failure_releases_the_item_for_a_new_launch_or_publish(launch):
    r = launch
    ident, revision = await configured(r)
    r.failure = "before"
    op = await r.launch.queue(7, ident, revision)
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "PERMANENT_FAILED"
    assert r.api.item["status"] == "DRAFT"
    # The failed launch must not leave live auto-delivery behind (gift listing armed
    # before publication has to be disarmed together with the draft release).
    assert not (r.service.listing("item") or {}).get("enabled")
    # A retry queues a NEW operation (revision was bumped by the release).
    draft = r.store.get_draft(ident, 7)
    r.failure = None
    value = draft["payload"]
    value["ready"] = await r.launch.check(7, value)
    r.store.update_draft(ident, 7, draft["revision"], value)
    op2 = await r.launch.queue(7, ident, r.store.get_draft(ident, 7)["revision"])
    assert op2 != op
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op2,))[0]["status"] == "SUCCEEDED"
    assert r.publications and r.api.item["status"] == "APPROVED"


async def test_local_stop_before_publication_is_permanent_not_uncertain(launch):
    r = launch
    ident, revision = await configured(r)
    op = await r.launch.queue(7, ident, revision)
    r.service.stop(7)  # owner stops gift automation before the publish executes
    await r.worker.once()
    row = r.store.rows("SELECT status,error_code FROM outbox WHERE id=?", (op,))[0]
    assert row["status"] == "PERMANENT_FAILED", row
    assert not r.publications and r.api.item["status"] == "DRAFT"


async def test_premium_launch_with_prepared_auto_delivery(launch):
    r = launch
    budget = {"per_operation": 50, "daily": 100, "monthly": 1000, "max_operations_per_item": 20}
    ident, revision = await configured(
        r, mode="text", publication="PREMIUM", quote_id="premium", promotion_policy=budget
    )
    op = await r.launch.queue(7, ident, revision)
    assert r.store.rows("SELECT kind FROM outbox WHERE id=?", (op,))[0]["kind"] == "premium_publish"
    assert not r.publications and not r.remote.payments
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "SUCCEEDED"
    assert r.publications and r.api.item["status"] == "APPROVED"
    saved = r.publications[0]["policy"][0]
    assert json.loads(saved["payload"])["enabled"] is True
    assert r.service.discover() == 0  # nothing to enroll before a sale
    # No bump was dispatched: the only expense record is the launch reservation itself.
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind='premium_bump'")


async def test_premium_launch_with_paid_promotion_prepared_before_publication(launch):
    r = launch
    budget = {"per_operation": 50, "daily": 100, "monthly": 1000, "max_operations_per_item": 20}
    ident, revision = await configured(
        r, mode="text", publication="PREMIUM", quote_id="premium", promotion=True, promotion_policy=budget
    )
    op = await r.launch.queue(7, ident, revision)
    assert not r.publications and not r.remote.payments
    prepared = r.store.setting("launch:prepared:item")
    assert prepared and prepared["operation"] == op
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "SUCCEEDED"
    assert r.publications and r.api.item["status"] == "APPROVED"
    # Promotion policy armed before publication; no bump was executed yet (freshly
    # published item, zero schedule ticks).
    assert r.store.setting("promotions:approved:item", {}).get("item", {}).get("id") == "item"
    assert not r.remote.payments


async def test_empty_stock_blocks_launch_before_any_operation(launch):
    r = launch
    ident = await r.launch.open(7, "item", {"delivery": "stock", "delivery_data": ""})
    draft = r.launch.load(7, ident)
    value = draft["payload"]
    with pytest.raises(ValidationError):
        await r.launch.check(7, value)
    assert not r.store.rows("SELECT 1 FROM outbox WHERE operation_key LIKE 'launch:%'")
    assert not r.publications and r.api.item["status"] == "DRAFT"


async def test_dispatcher_configuration_restart_preview_and_final_button(launch):
    r = launch
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("launch:item:item")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:mode:gifts")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:edit:after")
        ui.dp = dispatcher(r.store, r.api)
        await ui.message("Thank you")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:check")
        assert "15" in session.calls[-1].text and "готова" in session.calls[-1].text
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click(f"ln:{nonce}:commit")
        await ui.click(f"ln:{nonce}:commit")
        assert len(r.store.rows("SELECT * FROM outbox WHERE kind='publish'")) == 1
        assert not r.remote.payments and not r.publications
        assert "secret" not in session.calls[-1].text
    finally:
        await bot.session.close()
