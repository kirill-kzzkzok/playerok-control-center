"""Production logical binding → GiftService → Outbox against API/MTProto boundaries."""

import copy

import pytest

from app.gifts.worker import GiftWorker
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from tests.test_gifts import rig as gift_rig


@pytest.fixture(name="rig")
def _rig(tmp_path):
    yield from gift_rig.__wrapped__(tmp_path)


async def bind_logical(r):
    r.store.set_setting("playerok:account_id", "shop")
    r.api.item["price"] = 100

    def update_item(id, **changes):
        assert id == r.api.item["id"]
        r.api.item.update(changes)
        return copy.deepcopy(r.api.item)

    r.api.account.update_item = update_item
    registry = LogicalProducts(r.store, r.api)
    prepared = await registry.prepare(7, "item", "telegram_gifts", {"code": "heart", "after": "Thank you!"})
    result = registry.commit(7, prepared)
    await r.worker.once()
    product = registry.product(result["product_id"])
    assert product["state"] == "VERIFIED"
    assert "gift:" not in r.api.item["description"]
    assert r.api.item["name"].endswith("[" + product["code"] + "]")
    await registry.arm(7, product["id"], product["revision"], inherit=True)
    assert not r.remote.payments
    return registry, product


async def test_logical_gift_without_public_gift_tag_has_real_intent_and_receipt(rig):
    r = rig
    _registry, product = await bind_logical(r)
    Orders(r.store).ingest(r.api.add_order())
    assert r.service.discover() == 1
    await r.worker.once()
    assert len(r.remote.payments) == 1
    assert r.service.job("order")["state"] == "SUCCEEDED"
    scope = r.store.rows("SELECT * FROM logical_order_scopes")[0]
    assert scope["product_id"] == product["id"]
    assert r.service.discover() == 0
    r.store.set_setting("gifts:listing_options:item", {"after": "Different future message"})
    followups = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await followups.once()
    await r.worker.once()
    assert r.api.sent == [("chat-order", "Thank you!")]
    await followups.once()
    assert not await r.worker.once()
    assert len(r.api.sent) == 1


async def test_paid_order_can_deliver_after_its_verified_listing_becomes_sold(rig):
    r = rig
    await bind_logical(r)
    Orders(r.store).ingest(r.api.add_order())
    assert r.service.discover() == 1
    r.api.item["status"] = "SOLD"
    await r.worker.once()
    assert len(r.remote.payments) == 1
    assert r.service.job("order")["state"] == "SUCCEEDED"


async def test_logical_gift_limits_are_validated_and_applied_without_global_changes(rig):
    r = rig
    registry, product = await bind_logical(r)
    registry.stop(7, product["id"])
    with pytest.raises(ValueError):
        await registry.prepare(
            7, "item", "telegram_gifts", {"code": "heart", "options": {"order": 5, "day": 50}}
        )
    prepared = await registry.prepare(
        7, "item", "telegram_gifts", {"code": "heart", "options": {"order": 30, "day": 50}}
    )
    result = registry.commit(7, prepared)
    await r.worker.once()
    product = registry.product(result["product_id"])
    await registry.arm(7, product["id"], product["revision"])
    assert r.service.limits("item") == (30, 50)
    assert r.service.limits() == (100, 100)
    assert not r.remote.payments


async def test_reconfiguration_does_not_disarm_or_rewrite_unfinished_intent(rig):
    r = rig
    registry, product = await bind_logical(r)
    Orders(r.store).ingest(r.api.add_order())
    assert r.service.discover() == 1
    before = r.store.rows("SELECT * FROM gift_jobs")
    registry.stop(7, product["id"])
    with pytest.raises(ValueError):
        await registry.prepare(7, "item", "telegram_gifts", {"code": "heart"})
    assert r.store.rows("SELECT * FROM gift_jobs") == before
    assert not r.remote.payments


async def test_old_gift_controls_cannot_report_logically_stopped_product_as_working(rig):
    r = rig
    registry, product = await bind_logical(r)
    registry.stop(7, product["id"])
    await r.service.arm(7, "item", r.service.listing("item")["revision"])
    assert "выдача: Работает" not in r.service.status_text("item")
    Orders(r.store).ingest(r.api.add_order())
    assert r.service.discover() == 0
    assert not r.remote.payments


async def test_legacy_active_gift_cannot_be_reconfigured_without_stop(rig):
    r = rig
    r.api.item["price"] = 100
    r.store.set_setting("playerok:account_id", "shop")
    await r.service.bind(7, "item", "heart")
    await r.service.arm(7, "item", r.service.listing("item")["revision"])
    registry = LogicalProducts(r.store, r.api)
    registry.migrate_legacy()
    with pytest.raises(ValueError):
        await registry.prepare(7, "item", "fixed_text", {"text": "Other delivery"})
    assert r.service.listing("item")["enabled"] == 1
    assert not r.store.rows("SELECT * FROM gift_jobs")


async def test_logical_refund_requires_order_snapshot_permission_even_if_old_panel_changes_options(rig):
    from telethon import errors

    r = rig
    await bind_logical(r)
    r.api.support_refund = True
    r.remote.resolve_error = errors.UsernameNotOccupiedError(None)
    Orders(r.store).ingest(r.api.add_order())
    assert r.service.discover() == 1
    r.store.set_setting("gifts:listing_options:item", {"refund": True})
    await r.worker.once()
    assert r.service.job("order")["state"] == "INVALID_RECIPIENT"
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind='gift_refund'")
    assert not r.api.refunds and not r.remote.payments
