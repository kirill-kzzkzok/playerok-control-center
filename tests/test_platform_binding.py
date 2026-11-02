from copy import deepcopy

import pytest

from app.playerok.adapter import APIError
from app.services.automation import Automation
from app.services.logical_products import LogicalProducts
from app.workers.logical_products import LogicalProductWorker
from tests.test_playerok_delivery import drain, purchase
from tests.test_telegram_transport import production_dispatcher
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_universal_delivery_e2e import simulation as simulation_fixture
from tests.test_ux_redesign import buttons


@pytest.fixture
async def shop(tmp_path):
    async for factory in simulation_fixture.__wrapped__(tmp_path):
        yield factory


async def save(r, *, expected_error=False):
    await r.ui.click("platform:item:lot")
    await click(r, suffix=":reply")
    await click(r, suffix=":fulfill")
    if expected_error:
        await r.ui.click(next(k for k in buttons(r.session.last_panel) if k.endswith(":save")))
    else:
        await click(r, suffix=":save")


@pytest.mark.parametrize("status", ["APPROVED", "DRAFT", "SOLD", "EXPIRED"])
async def test_new_code_waits_for_readback_then_future_followup_without_goods(shop, status):
    r = await shop()
    r.api.item.update(status=status, is_editable=True)
    await save(r)
    assert "Будет добавлен постоянный код" in r.session.last_panel.text
    assert not r.store.rows("SELECT * FROM delivery_policies")
    commit = await click(r, suffix=":bind")
    assert not Automation(r.store, r.api).get("lot")[1].enabled
    assert LogicalProducts(r.store, r.api).listing("lot")["state"] == "PENDING"
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.click(commit)
    assert len(r.store.rows("SELECT * FROM outbox")) == 1
    await r.worker.once()
    product = LogicalProducts(r.store, r.api).listing("lot")
    assert product["state"] == "VERIFIED" and product["inherit_enabled"]
    assert product["enabled"] and Automation(r.store, r.api).get("lot")[1].enabled
    assert r.api.item["description"].splitlines()[-1] == product["code"]
    await r.ui.click("platform:item:lot")
    assert "Привязка подтверждена" in r.session.last_panel.text
    assert product["code"] in r.session.last_panel.text
    r.api.item["status"] = "APPROVED"
    purchase(r)
    await drain(r)
    assert len(r.api.messages) == 1 and r.api.orders["order"]["status"] == "SENT"
    assert not r.api.remote.payments
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind IN ('delivery','delivery_text','gift_send')")


async def test_forbidden_edit_never_creates_or_enables_new_policy(shop):
    r = await shop()
    r.api.item["is_editable"] = False
    await save(r, expected_error=True)
    assert "запрещает редактирование" in r.session.last_panel.text
    assert not r.store.rows("SELECT * FROM logical_products")
    assert not r.store.rows("SELECT * FROM delivery_policies")


async def test_edit_permission_revoked_after_confirmation_never_arms(shop):
    r = await shop()
    await save(r)
    await click(r, suffix=":bind")
    r.api.item["is_editable"] = False
    await r.worker.once()
    assert not r.api.mutations
    assert r.store.rows("SELECT status,started_at FROM outbox") == [
        {"status": "PERMANENT_FAILED", "started_at": None}
    ]
    assert not Automation(r.store, r.api).get("lot")[1].enabled


async def test_lost_edit_ack_stays_uncertain_disabled_and_no_rewrite(shop):
    r = await shop()
    await save(r)
    await click(r, suffix=":bind")
    original = r.api.account.update_item
    calls = []

    def lost(**kwargs):
        calls.append(kwargs)
        original(**kwargs)
        raise APIError("NETWORK_ERROR", uncertain=True)

    r.api.account.update_item = lost
    await r.worker.once()
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.click("platform:item:lot")
    assert "неизвест" in r.session.last_panel.text.lower()
    assert not await r.worker.once()
    assert len(calls) == 1 and not Automation(r.store, r.api).get("lot")[1].enabled
    assert r.store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
    await r.ui.click("lp:item:lot")
    reconcile = await click(r, suffix=":reconcile")
    product = LogicalProducts(r.store, r.api).listing("lot")
    assert product["state"] == "VERIFIED" and product["enabled"]
    assert Automation(r.store, r.api).get("lot")[1].enabled
    revision = product["revision"]
    await r.ui.click(reconcile)
    assert LogicalProducts(r.store, r.api).listing("lot")["revision"] == revision
    assert len(calls) == 1 and not await r.worker.once()
    operation = r.store.rows("SELECT id FROM outbox")[0]["id"]
    await r.ui.click("result:show:" + operation)
    assert "Привязка подтверждена" in r.session.last_panel.text
    assert "Автоответ: включён" in r.session.last_panel.text


async def test_existing_gift_code_requires_explicit_switch_and_keeps_identity(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    before = LogicalProducts(r.store, r.api).listing("lot")
    count = len(r.store.rows("SELECT id FROM outbox"))
    await save(r)
    assert "Telegram Gifts" in r.session.last_panel.text and "замен" in r.session.last_panel.text.lower()
    assert r.store.rows("SELECT enabled FROM gift_listings") == [{"enabled": 1}]
    await click(r, suffix=":switch")
    after = LogicalProducts(r.store, r.api).listing("lot")
    assert (after["id"], after["code"]) == (before["id"], before["code"])
    assert len(r.store.rows("SELECT id FROM outbox")) == count
    assert r.store.rows("SELECT enabled FROM gift_listings") == [{"enabled": 0}]
    assert Automation(r.store, r.api).get("lot")[1].enabled
    await r.ui.click("platform:item:lot")
    await click(r, suffix=":save")
    assert len(r.store.rows("SELECT id FROM outbox")) == count
    assert len(r.store.rows("SELECT id FROM logical_products")) == 1


async def test_global_stop_while_binding_pending_never_arms_after_readback(shop):
    r = await shop()
    await save(r)
    await click(r, suffix=":bind")
    Automation(r.store, r.api).stop_all(7)
    await r.worker.once()
    assert not Automation(r.store, r.api).get("lot")[1].enabled
    assert not LogicalProducts(r.store, r.api).listing("lot")["enabled"]


async def test_new_binding_inherits_private_partial_paid_snapshot(shop):
    r = await shop()
    await save(r)
    await click(r, suffix=":bind")
    await r.worker.once()
    r.api.item["status"] = "SOLD"
    full = r.api.order("order")
    full["is_automated"] = True
    full["item"]["id"] = "private"
    r.api.items["private"] = deepcopy(full["item"])
    from app.services.orders import Orders

    Orders(r.store).ingest(full)
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_item" and kwargs["id"] == "private":
            raise APIError("HTTP_404")
        if method == "get_delivery_deal":
            return deepcopy(full)
        if method == "get_deal":
            return {**deepcopy(full), "chat": None}
        return await original(method, **kwargs)

    r.api.call = call
    await LogicalProductWorker(r.store, r.api).once()
    assert LogicalProducts(r.store, r.api).listing("private")["source_order_id"] == "order"
    await drain(r)
    assert len(r.api.messages) == 1
    assert r.api.orders["order"]["status"] == "SENT"
    assert not r.api.remote.payments
