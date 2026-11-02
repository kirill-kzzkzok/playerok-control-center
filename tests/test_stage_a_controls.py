"""Stage A: real Dispatcher, SQLite and workers; only remote boundaries are fake."""

import copy

import pytest

from app.gifts.service import GiftService
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.services.product_readiness import ProductReadiness
from app.workers.logical_products import LogicalProductWorker
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_universal_delivery_e2e import simulation as simulation_fixture
from tests.test_ux_redesign import buttons


@pytest.fixture
async def shop(tmp_path):
    async for create in simulation_fixture.__wrapped__(tmp_path):
        yield create


async def test_price_change_stop_restart_and_worker_agree(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    logical = LogicalProducts(r.store, r.api)
    original = dict(logical.listing("lot"))
    r.api.item.update(price=90, discount=10, attachments=[{"id": "new-photo"}])
    await click(r, action="rd:check:lot")
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    assert result["ready"], result["problems"]
    await LogicalProductWorker(r.store, r.api).once()
    assert not logical.listing("lot")["error_code"]
    logical.require_delivery("lot", "telegram_gifts")
    await click(r, action="item:lot")
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":stop")
    assert not logical.listing("lot")["enabled"]
    assert not GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender).listing("lot")["enabled"]
    await click(r, action="lp:item:lot")
    await click(r, suffix=":arm")
    await click(r, suffix=":enable:inherit")
    current = logical.listing("lot")
    assert current["id"] == original["id"] and current["config"] == original["config"]
    assert current["enabled"]
    service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    Orders(r.store).ingest(r.api.order("new-paid"))
    assert service.discover() == 1
    await r.worker.once()
    assert service.job("new-paid")["state"] == "SUCCEEDED"
    await r.worker.once()
    assert service.discover() == 0 and len(r.api.remote.payments) == 1


@pytest.mark.parametrize("logical_enabled,gift_enabled", [(1, 0), (0, 1)])
async def test_divergent_projection_can_be_repaired_through_ui(shop, logical_enabled, gift_enabled):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    # Reproduce persisted legacy divergence; no business service is replaced.
    r.store.execute("UPDATE logical_products SET enabled=?", (logical_enabled,))
    r.store.execute("UPDATE gift_listings SET enabled=?", (gift_enabled,))
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":arm")
    await click(r, suffix=":enable:inherit")
    assert LogicalProducts(r.store, r.api).listing("lot")["enabled"] == 1
    assert r.store.rows("SELECT enabled FROM gift_listings")[0]["enabled"] == 1
    assert ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)["ready"]
    assert not r.api.remote.payments and not r.api.messages
    service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    Orders(r.store).ingest(r.api.order("after-repair"))
    assert service.discover() == 1
    await r.worker.once()
    assert service.job("after-repair")["state"] == "SUCCEEDED"
    assert service.discover() == 0 and len(r.api.remote.payments) == 1


@pytest.mark.parametrize("field", ["name", "description", "category", "obtaining_type", "data_fields"])
async def test_critical_change_remains_blocked_with_specific_reason(shop, field):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    if field in {"name", "description"}:
        r.api.item[field] = "Changed " + r.api.item[field]
    elif field == "data_fields":
        r.api.item[field] = [{"id": "changed-recipient", "value": "different"}]
    else:
        r.api.item[field] = {"id": "changed"}
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=copy.deepcopy(r.api.item))
    assert not result["ready"]
    problem = next(p for p in result["problems"] if p["code"] == "ITEM_CHANGED")
    assert field not in problem["text"]  # Owner text is Russian, diagnostic codes stay internal.
    assert not r.api.remote.payments


async def test_temporary_pause_preserves_intent_and_cannot_clear_emergency_stop(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    before = r.store.rows("SELECT * FROM gift_listings")
    product = dict(LogicalProducts(r.store, r.api).listing("lot"))
    await r.ui.click("gft:settings")
    await click(r, action="gft:pause")
    service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    assert not service.runtime()
    assert r.store.rows("SELECT * FROM gift_listings") == before
    assert dict(LogicalProducts(r.store, r.api).listing("lot")) == product
    assert "GIFTS_PAUSE" in {
        p["code"] for p in ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)["problems"]
    }
    await click(r, action="gft:unpause")
    assert service.runtime()
    await click(r, action="gft:pause")
    service.stop(7)
    await click(r, action="gft:unpause")
    assert r.store.setting("gifts:stopped") and not service.runtime()
    assert not LogicalProducts(r.store, r.api).listing("lot")["enabled"]
    assert not r.api.remote.payments


async def test_critical_error_offers_actual_configuration_action(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":stop")
    r.api.item["description"] += " changed"
    await click(r, action="lp:item:lot")
    await click(r, suffix=":arm")
    assert "Изменилось описание" in r.session.last_panel.text
    await click(r, suffix=":configure")
    assert "Способ выдачи" in r.session.last_panel.text
    assert any(k.endswith(":mode:telegram_gifts") for k in buttons(r.session.last_panel))


async def test_second_read_critical_edit_cannot_enable_text_projection(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "isolated text")
    logical = LogicalProducts(r.store, r.api)
    product = logical.listing("lot")
    logical.stop(7, product["id"])
    call = r.api.call
    reads = 0

    async def changing_read(method, **kwargs):
        nonlocal reads
        if method == "get_item":
            reads += 1
            if reads == 2:
                r.api.item["description"] += " changed during check"
        return await call(method, **kwargs)

    r.api.call = changing_read
    with pytest.raises(ValueError, match="описание"):
        await logical.arm(7, product["id"], product["revision"])
    assert not logical.listing("lot")["enabled"]
    assert not ProductReadiness(r.store, r.api).automation.get("lot")[1].enabled


async def test_explicit_check_recovers_stale_price_blocker_while_stopped(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    logical = LogicalProducts(r.store, r.api)
    product = logical.listing("lot")
    logical.stop(7, product["id"])
    r.api.item["price"] = 90
    r.store.execute("UPDATE logical_products SET error_code='RELIST_CONFIRMATION_REQUIRED'")
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":arm")
    await click(r, suffix=":enable:inherit")
    assert not logical.listing("lot")["error_code"]
    assert logical.listing("lot")["enabled"]
    assert not r.api.remote.payments
