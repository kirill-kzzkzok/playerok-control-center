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
    assert r.api.item["name"].endswith("[" + product["code"] + "]")
    assert r.api.item["description"] == "Original description"
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


@pytest.mark.parametrize("previous_conflict", [False, True])
async def test_platform_listing_data_change_keeps_binding_and_future_followup(shop, previous_conflict):
    r = await shop()
    r.api.item["data_fields"] = [{"id": "details", "value": "old fixture"}]
    await save(r)
    await click(r, suffix=":bind")
    await r.worker.once()
    logical = LogicalProducts(r.store, r.api)
    before = logical.listing("lot")
    r.api.item["data_fields"] = [{"id": "details", "value": "new fixture"}]
    if previous_conflict:
        LogicalProductWorker(r.store, r.api).problem(before, "RELIST_BINDING_CONFLICT", candidate_id="lot")

    await LogicalProductWorker(r.store, r.api).once()
    after = logical.listing("lot")
    assert after["error_code"] is None
    for key in ("id", "code", "revision", "control_revision", "enabled", "armed_at", "baseline", "config"):
        assert after[key] == before[key]
    assert len(r.api.mutations) == 1  # Only the original test binding edit.
    purchase(r)
    await drain(r)
    assert len(r.api.messages) == 1 and r.api.orders["order"]["status"] == "SENT"
    assert not r.api.remote.payments
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind IN ('delivery','delivery_text','gift_send')")


@pytest.mark.parametrize("conflict", ["obtaining", "relisted_fields", "duplicate"])
async def test_platform_field_exception_does_not_adopt_conflicting_listing(shop, conflict):
    r = await shop()
    await save(r)
    await click(r, suffix=":bind")
    await r.worker.once()
    if conflict == "obtaining":
        r.api.item["obtaining_type"] = {"id": "different-method"}
    else:
        r.api.items["another"] = {**deepcopy(r.api.item), "id": "another"}
        if conflict == "relisted_fields":
            r.api.item["status"] = "SOLD"
            r.api.items["another"]["data_fields"] = [{"id": "details", "value": "different fixture"}]
    logical = LogicalProducts(r.store, r.api)
    await LogicalProductWorker(r.store, r.api).once()
    assert (
        logical.listing("lot")["error_code"]
        == {
            "obtaining": "RELIST_BINDING_CONFLICT",
            "relisted_fields": "RELIST_CRITICAL_FIELDS_CHANGED",
            "duplicate": "DUPLICATE_ACTIVE_MARKER",
        }[conflict]
    )
    assert not logical.listing("another")
    assert len(r.api.mutations) == 1 and not r.api.messages and not r.api.remote.payments


async def test_platform_card_names_only_bot_pause_and_keeps_binding_reason(shop):
    r = await shop()
    await save(r)
    await click(r, suffix=":bind")
    await r.worker.once()
    product = LogicalProducts(r.store, r.api).listing("lot")
    LogicalProductWorker(r.store, r.api).problem(product, "RELIST_BINDING_CONFLICT", candidate_id="lot")
    await r.ui.click("item:lot")
    text = r.session.last_panel.text
    assert "Товар выдаёт Playerok" in text
    assert "Действия бота после оплаты приостановлены" in text
    assert "Серверные данные объявления противоречат сохранённой привязке" in text
    assert "конфликт постоянного кода" not in text
    assert not r.api.messages and not r.api.remote.payments
    await r.ui.click("lp:details:lot")
    assert "Действия бота после оплаты приостановлены" in r.session.last_panel.text
    await click(r, suffix=":stop")
    assert "Остановить действия бота после оплаты?" in r.session.last_panel.text
    await click(r, suffix=":stop_confirm")
    assert "Действия бота после оплаты остановлены" in r.session.last_panel.text
    assert len(r.api.mutations) == 1


async def test_platform_card_names_missing_code_after_fresh_read(shop):
    from app.services.product_readiness import ProductReadiness

    r = await shop()
    await save(r)
    await click(r, suffix=":bind")
    await r.worker.once()
    product = LogicalProducts(r.store, r.api).listing("lot")
    r.api.item["name"] = r.api.item["name"].rsplit(" [", 1)[0]
    item = await r.api.call("get_item", id="lot")
    ProductReadiness(r.store, r.api).remember_item(item)
    await r.ui.click("item:lot")
    assert f"В конце названия отсутствует код [{product['code']}]" in r.session.last_panel.text
    assert not r.api.messages and not r.api.remote.payments


async def test_unrelated_description_does_not_create_a_code_conflict(shop):
    r = await shop()
    await save(r)
    await click(r, suffix=":bind")
    await r.worker.once()
    logical = LogicalProducts(r.store, r.api)
    product = logical.listing("lot")
    r.api.items["other"] = {
        **deepcopy(r.api.item),
        "id": "other",
        "name": "Unrelated listing [malformed]",
        "description": f"Reference number {product['code']}",
    }
    await LogicalProductWorker(r.store, r.api).once()
    assert logical.listing("lot")["error_code"] is None
    assert not logical.listing("other")
    r.api.items["other"]["name"] = f"Other listing [{product['code']}] with a misplaced marker"
    await LogicalProductWorker(r.store, r.api).once()
    assert logical.listing("lot")["error_code"] == "CATALOG_MARKER_INVALID"
    assert not logical.listing("other")
    assert len(r.api.mutations) == 1 and not r.api.remote.payments


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
