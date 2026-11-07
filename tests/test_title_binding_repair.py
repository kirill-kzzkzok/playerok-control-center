"""External rename -> owner confirmation -> actual worker, no live APIs."""

import json
from copy import deepcopy

import pytest

from app.gifts.worker import GiftWorker
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from tests.test_stage_a_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def renamed(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    r.api.item["name"] = "Новое название " + r.api.item["name"]
    return r


async def test_title_only_confirmation_preserves_code_authority_and_delivers_once(shop):
    from tests.test_unified_delivery_controls import state

    r = await renamed(shop)
    service = LogicalProducts(r.store, r.api)
    ident = r.store.enqueue("closed-title", "edit", "lot", {"name": r.api.item["name"]}, actor=7)
    action = r.store.claim(operation_id=ident)
    r.store.mark_started(action)
    r.store.finish(ident, action["lease_token"], "UNCERTAIN", error="EDIT_NOT_CONFIRMED")
    await r.ui.click("ops:stop:" + ident)
    await r.ui.message("Закрываю запись правки без повторной отправки")
    resolution = (await state(r).get_data())["resolution"]
    await r.ui.click("ops:apply:" + resolution["nonce"])
    before = service.listing("lot")
    gifts = r.store.rows("SELECT * FROM gift_listings")
    journal = r.store.rows("SELECT * FROM outbox")
    await r.ui.click("item:lot")
    await r.ui.click("rd:check:lot")
    # New standing permission removes the previous required owner confirmation.
    assert "lp:title:lot" not in buttons(r.session.last_panel)
    current = service.listing("lot")
    assert current["title_snapshot"] == r.api.item["name"]
    for key in (
        "id",
        "code",
        "config",
        "revision",
        "control_revision",
        "enabled",
        "verified_at",
        "armed_at",
        "inherit_enabled",
    ):
        assert current[key] == before[key]
    assert r.store.rows("SELECT * FROM gift_listings") == gifts
    assert r.store.rows("SELECT * FROM outbox") == journal
    await r.ui.click("item:lot")  # Reopening cannot repeat a payment.
    Orders(r.store).ingest(r.api.order("after-rename"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    await r.worker.once()
    assert len(r.api.remote.payments) == 1
    assert r.api.remote.payments[0].invoice.gift_id == r.api.remote.catalog["heart"].id
    assert r.api.remote.payments[0].invoice.message is None


async def test_configure_blocked_enabled_product_offers_stop_and_continue(shop):
    r = await renamed(shop)
    original = LogicalProducts(r.store, r.api).listing("lot")
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":configure")
    assert not r.session.last_panel.text.startswith("❗")
    await click(r, suffix=":stopconfigure")
    await click(r, suffix=":stop_confirm")
    assert "Способ выдачи" in r.session.last_panel.text
    assert not LogicalProducts(r.store, r.api).listing("lot")["enabled"]
    assert not r.store.rows("SELECT 1 FROM gift_listings WHERE enabled=1")
    assert not r.api.remote.payments
    await click(r, suffix=":mode:telegram_gifts")
    await click(r, suffix=":giftclear")
    await click(r, suffix=":giftpick:bear")
    await click(r, suffix=":giftdone")
    await click(r, suffix=":preview")
    await click(r, suffix=":commit")
    await r.worker.once()
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":arm")
    await click(r, suffix=":enable:inherit")
    current = LogicalProducts(r.store, r.api).listing("lot")
    assert current["id"] == original["id"] and current["code"] == original["code"]
    assert json.loads(current["config"])["code"] == "bear"
    assert current["enabled"] and not r.api.remote.payments
    Orders(r.store).ingest(r.api.order("new-bear-order"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert len(r.api.remote.payments) == 1
    assert r.api.remote.payments[0].invoice.gift_id == r.api.remote.catalog["bear"].id
    assert r.api.remote.payments[0].invoice.message is None
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]


@pytest.mark.parametrize(
    "change", ["name", "description", "category", "code", "stop", "unknown", "config", "fields", "account"]
)
async def test_title_confirmation_rejects_changes_after_preview(shop, change):
    r = await renamed(shop)
    service = LogicalProducts(r.store, r.api)
    await r.ui.click("lp:title:lot")
    confirm = next(k for k in buttons(r.session.last_panel) if k.endswith(":title_apply"))
    old_title = service.listing("lot")["title_snapshot"]
    if change == "stop":
        service.stop(7, service.listing("lot")["id"])
    elif change == "config":
        service.update_after(7, "lot", service.listing("lot"), "Другой текст благодарности")
    elif change == "fields":
        r.api.item["data_fields"] = [{"id": "changed-recipient", "value": "another"}]
    elif change == "account":
        r.api.item["user"]["id"] = "another-seller"
    elif change == "unknown":
        ident = r.store.enqueue("unknown-title", "edit", "lot", {"name": r.api.item["name"]}, actor=7)
        action = r.store.claim(operation_id=ident)
        r.store.mark_started(action)
        r.store.finish(ident, action["lease_token"], "UNCERTAIN", error="EDIT_NOT_CONFIRMED")
    elif change == "category":
        r.api.item["category"] = {"id": "different"}
    elif change == "code":
        r.api.item["name"] = "Другой товар [ZZZ]"
    else:
        r.api.item[change] += " changed again"
    await r.ui.click(confirm)
    assert r.session.last_panel.text.startswith("❗")
    assert service.listing("lot")["title_snapshot"] == old_title
    assert not r.api.remote.payments


async def test_confirmed_rename_preserves_global_stop(shop):
    from app.services.product_readiness import ProductReadiness

    r = await renamed(shop)
    await r.ui.click("lp:title:lot")
    r.store.set_setting("gifts:stopped", True)
    await click(r, suffix=":title_apply")
    assert r.store.setting("gifts:stopped")
    assert not ProductReadiness(r.store, r.api).snapshot("lot")["ready"]
    Orders(r.store).ingest(r.api.order("stopped-after-title"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert not r.api.remote.payments and r.store.setting("gifts:stopped")


async def test_confirmed_rename_preserves_relist_inheritance(shop):
    from app.workers.logical_products import LogicalProductWorker

    r = await renamed(shop)
    service = LogicalProducts(r.store, r.api)
    original = service.listing("lot")
    await r.ui.click("lp:title:lot")
    await click(r, suffix=":title_apply")
    r.api.item["status"] = "SOLD"
    r.api.items["next"] = {**deepcopy(r.api.item), "id": "next", "status": "APPROVED"}
    await LogicalProductWorker(r.store, r.api).once()
    adopted = service.listing("next")
    assert adopted["id"] == original["id"] and adopted["code"] == original["code"]
    assert not r.api.remote.payments


@pytest.mark.parametrize("revoke", [True, False])
async def test_authority_changed_during_server_read_cannot_commit_title(shop, revoke):
    r = await renamed(shop)
    service = LogicalProducts(r.store, r.api)
    await r.ui.click("lp:title:lot")
    old_title = service.listing("lot")["title_snapshot"]
    confirm = next(k for k in buttons(r.session.last_panel) if k.endswith(":title_apply"))
    original = r.api.call

    async def change_authority(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_item":
            if revoke:
                r.store.execute("DELETE FROM administrators WHERE user_id=7")
            else:
                r.api.account.id = "different-account"
        return result

    r.api.call = change_authority
    await r.ui.click(confirm)
    assert service.listing("lot")["title_snapshot"] == old_title
    assert not r.api.remote.payments


async def test_title_repair_after_background_scan_conflict_rechecks_catalog(shop):
    from app.workers.logical_products import LogicalProductWorker

    r = await renamed(shop)
    service = LogicalProducts(r.store, r.api)
    await LogicalProductWorker(r.store, r.api).once()
    assert service.listing("lot")["error_code"] is None
    await r.ui.click("lp:title:lot")
    await click(r, suffix=":title_apply")
    assert service.listing("lot")["error_code"] is None
    assert service.listing("lot")["title_snapshot"] == r.api.item["name"]
    service.require_delivery("lot", "telegram_gifts")
    assert not r.api.remote.payments
