"""Safe marketing text is refreshed by production workers, never by owner repair clicks."""

from copy import deepcopy

import pytest

from app.gifts.worker import GiftWorker
from app.playerok.adapter import APIError
from app.services.logical_products import LogicalProducts, digest
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.logical_products import LogicalProductWorker
from tests.test_stage_a_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


@pytest.mark.parametrize(
    "handler,value",
    [("telegram_gifts", "gift: heart"), ("unique_stock", None), ("fixed_text", "unique-content")],
)
@pytest.mark.parametrize("relisted", [False, True])
async def test_metadata_edit_recovers_automatically_without_changing_delivery_authority(
    shop, handler, value, relisted
):
    r = await shop(gift=handler == "telegram_gifts")
    await setup_ui(r, handler, value)
    service = LogicalProducts(r.store, r.api)
    old = service.listing("lot")
    target = "new" if relisted else "lot"
    if relisted:
        r.api.item["status"] = "SOLD"
        r.api.items[target] = {**deepcopy(r.api.item), "id": target, "status": "APPROVED"}
    item = r.api.items[target]
    item.update(description="New marketing description without changing delivery data", price=90)
    item["name"] = "Updated offer " + item["name"]
    await LogicalProductWorker(r.store, r.api).once()
    current = service.listing(target)
    assert current and not current["error_code"]
    assert current["description_sha256"] == digest(item["description"])
    for field in ("id", "code", "config", "armed_at", "enabled", "revision", "control_revision"):
        assert current[field] == old[field]
    Orders(r.store).ingest(r.api.order("new-sale", target))
    for _ in range(6):
        if handler == "telegram_gifts":
            await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        else:
            await EnrollmentWorker(r.store, r.api).once()
            ChainWorker(r.store, r.api).once()
        await r.worker.once()
    if handler == "telegram_gifts":
        assert len(r.api.remote.payments) == 1
        assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    else:
        assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "SUCCEEDED"}]
    assert len(r.api.mutations) == 1  # Initial explicit binding only; no live text rewrite.


@pytest.mark.parametrize(
    "fault",
    [
        "category",
        "code",
        "receiver",
        "duplicate",
        "stop",
        "network",
        "race",
        "unknown_edit",
        "projection",
        "missing",
    ],
)
async def test_metadata_refresh_preserves_critical_guards_stop_and_both_projection_snapshots(shop, fault):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    service = LogicalProducts(r.store, r.api)
    before = service.listing("lot")
    gift_before = r.store.rows("SELECT * FROM gift_listings")[0]
    r.api.item["description"] = "New ordinary product description"
    if fault == "category":
        r.api.item["category"] = {"id": "different-category"}
    elif fault == "code":
        r.api.item["name"] = "A product without the confirmed code"
    elif fault == "receiver":
        r.api.item["data_fields"] = [{"id": "recipient-critical", "value": "changed"}]
    elif fault == "duplicate":
        r.api.items["duplicate"] = {**deepcopy(r.api.item), "id": "duplicate"}
    elif fault == "unknown_edit":
        op = r.store.enqueue("unknown-title", "edit", "lot", {"name": r.api.item["name"]}, actor=7)
        action = r.store.claim()
        r.store.mark_started(action)
        r.store.finish(op, action["lease_token"], "UNCERTAIN")
    elif fault == "projection":
        r.store.execute("UPDATE gift_listings SET gift_code='bear'")
    original, reads = r.api.call, []

    async def boundary(method, **kwargs):
        if method == "get_item":
            reads.append(method)
            if len(reads) == 3:
                if fault == "stop":
                    service.stop(7, before["id"])
                elif fault == "network":
                    raise APIError("NETWORK_ERROR")
                elif fault == "race":
                    r.api.item["description"] += " changed while checking"
        value = await original(method, **kwargs)
        if method == "get_item" and fault == "missing":
            value.pop("description", None)
        return value

    r.api.call = boundary
    try:
        await LogicalProductWorker(r.store, r.api).once()
    except APIError:
        assert fault in {"network", "missing"}
    current = service.listing("lot")
    assert current["description_sha256"] == before["description_sha256"]
    assert current["title_snapshot"] == before["title_snapshot"]
    assert current["config"] == before["config"]
    assert (
        r.store.rows("SELECT description_sha256 FROM gift_listings")[0]["description_sha256"]
        == gift_before["description_sha256"]
    )
    if fault == "stop":
        assert not current["enabled"] and not r.store.rows("SELECT 1 FROM gift_listings WHERE enabled=1")
    assert not r.api.remote.payments and len(r.api.mutations) == 1
