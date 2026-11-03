"""Platform delivery belongs to the same creation and launch path."""

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from app.playerok.adapter import APIError
from app.services.automation import Automation
from app.services.logical_products import LogicalProducts
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_final_modern_gift_creation import shop as shop_fixture
from tests.test_gifts import Marketplace

shop = shop_fixture


async def test_main_creation_wizard_offers_playerok_delivery_without_inventory_or_stars(shop):
    r = shop
    await r.create_and_prepare(stop_before_delivery=True)
    choices = (await r.state.get_data())["choices"]
    assert {choice["value"] for choice in choices} == {"manual", "text", "stock", "gifts", "playerok"}
    assert not r.api.created and not r.remote.payments


async def platform_draft(r):
    create = r.api.account.create_item

    def server_create(**kwargs):
        create(**kwargs)
        r.api.item["comment"] = kwargs.get("comment")
        r.api.item["created_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        return deepcopy(r.api.item)

    r.api.account.create_item = server_create
    await r.create_and_prepare(
        stop_before_delivery=True, description="Товар передаёт покупателю сама площадка"
    )
    await r.wizard_action("choose:4")
    await r.wizard_action("confirm_done")
    ident = (await r.state.get_data())["draft_id"]
    await r.wizard_action("submit")
    await r.worker.once()
    product = LogicalProducts(r.store, r.api).listing("item")
    assert product and product["state"] == "VERIFIED" and json.loads(product["config"])["platform_only"]
    await r.ui.click("launch:from:" + ident)
    return ident


@pytest.mark.parametrize("reply,complete", [(False, False), (True, False), (False, True), (True, True)])
async def test_platform_draft_is_prepared_before_publication_and_never_resends_goods(shop, reply, complete):
    r = shop
    await platform_draft(r)
    published = []

    def publish(**kwargs):
        _, policy = Automation(r.store, r.api).get("item")
        assert policy.mode == "playerok" and policy.paid_reply == reply and policy.mark_sent == complete
        assert policy.enabled == (reply or complete)
        assert not r.store.rows("SELECT 1 FROM gift_listings WHERE enabled=1")
        published.append(kwargs)
        r.api.item["status"] = "APPROVED"
        order = r.api.add_order("instant-platform")
        order["created_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        order["is_automated"] = True
        return deepcopy(r.api.item)

    def mark(deal_id, new_status):
        r.api.orders[deal_id]["status"] = new_status.name
        return deepcopy(r.api.orders[deal_id])

    r.api.account.publish_item = publish
    r.api.account.update_deal = mark
    r.api.account.send_message = lambda chat_id, text: Marketplace.send_message(r.api, chat_id, text)
    if reply:
        await r.launch_action("platform_reply")
        await r.launch_action("edit:greeting")
        await r.ui.message("Спасибо за покупку на площадке")
    if complete:
        await r.launch_action("complete")
    if not (reply or complete):
        r.api.account.automation_enabled = False
    await r.launch_action("check")
    data = (await r.state.get_data())["launch"]
    draft = r.store.get_draft(data["id"], 7)
    assert draft["payload"].get("ready"), r.session.calls[-1].text
    await r.launch_action("commit")
    await r.worker.once()
    assert len(published) == 1 and r.api.item["status"] == "APPROVED"
    await r.sync.once()
    for _ in range(6):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert not r.store.rows("SELECT * FROM reservations") and not r.remote.payments
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind IN ('delivery','delivery_text','gift_send')")
    assert len(r.api.sent) == int(reply)
    assert r.api.orders["instant-platform"]["status"] == ("SENT" if complete else "PAID")
    assert not await r.worker.once()


async def test_global_stop_after_platform_ready_requires_a_new_owner_check(shop):
    r = shop
    await platform_draft(r)
    await r.launch_action("check")
    Automation(r.store, r.api).stop_all(7)
    await r.launch_action("commit")
    assert not r.store.rows("SELECT * FROM outbox WHERE kind='publish'")
    assert r.api.item["status"] == "DRAFT"
    assert not r.remote.payments


async def test_unknown_platform_publication_after_preparation_never_repeats(shop):
    r = shop
    await platform_draft(r)
    calls = []

    def lose_reply(**kwargs):
        calls.append(kwargs)
        r.api.item["status"] = "APPROVED"
        raise APIError("NETWORK_ERROR", uncertain=True)

    r.api.account.publish_item = lose_reply
    await r.launch_action("platform_reply")
    await r.launch_action("check")
    await r.launch_action("commit")
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE kind='publish'") == [{"status": "UNCERTAIN"}]
    r.restart()
    assert not await r.worker.once()
    assert len(calls) == 1 and not r.remote.payments and not r.api.sent


async def test_switch_from_gifts_needs_confirmation_and_cancel_preserves_intent(shop):
    r = shop
    await r.create_and_prepare()
    view = (await r.state.get_data())["launch"]
    before = r.store.get_draft(view["id"], 7)["payload"]
    assert before["mode"] == "gifts" and before["auto_complete"]
    await r.launch_action("mode:playerok")
    assert "Сменить Telegram Gifts" in r.session.calls[-1].text
    await r.launch_action("platform_cancel")
    cancelled = r.store.get_draft(view["id"], 7)["payload"]
    assert cancelled["mode"] == "gifts" and cancelled["auto_complete"]
    await r.launch_action("mode:playerok")
    await r.launch_action("platform_switch")
    confirmed = r.store.get_draft(view["id"], 7)["payload"]
    assert confirmed["mode"] == "playerok" and confirmed["platform_switch_confirmed"]
    assert not confirmed["auto_complete"] and not confirmed["platform_reply"]
    product = LogicalProducts(r.store, r.api).listing("item")
    assert product["handler"] == "telegram_gifts" and not product["enabled"]
    assert not r.api.published and not r.remote.payments
