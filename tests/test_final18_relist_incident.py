import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.playerok.adapter import APIError
from app.services.logical_products import LogicalProducts
from app.workers.logical_products import LogicalProductWorker
from app.workers.notifications import Notifications
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_short_identity_verification_does_not_warn_on_recovered_scan(shop, monkeypatch, caplog):
    r = await shop()
    worker = LogicalProductWorker(r.store, r.api)
    stop = asyncio.Event()
    failed = asyncio.Event()
    lock = asyncio.Lock()
    connection = SimpleNamespace(lock=lock, state={"status": "UNKNOWN", "reason": "CHECK_IN_PROGRESS"})
    r.api.connection = connection
    r.store.set_setting("runtime:cycle:logical-products", {"consecutive_failures": 2, "incident": "prior"})
    caplog.set_level("INFO", logger="pcc.shop")
    attempts = 0

    async def once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            failed.set()
            raise APIError("CONNECTION_NOT_VERIFIED")
        stop.set()

    monkeypatch.setattr(worker, "once", once)
    async with lock:
        task = asyncio.create_task(worker.run(stop))
        await failed.wait()
        connection.state = {"status": "CONNECTED", "reason": None}
    try:
        await asyncio.wait_for(task, 2)
    finally:
        stop.set()
    assert attempts == 2
    assert not any(record.levelno >= 30 for record in caplog.records)
    assert not r.store.rows("SELECT 1 FROM inbox WHERE kind='worker_failed'")


async def test_unresolved_scan_gate_is_diagnostic_without_duplicate_owner_warning(shop, monkeypatch, caplog):
    r = await shop()
    worker = LogicalProductWorker(r.store, r.api)
    stop = asyncio.Event()
    r.api.connection = SimpleNamespace(state={"status": "DISCONNECTED", "reason": "AUTH_REJECTED"})
    r.store.set_setting("runtime:cycle:logical-products", {"consecutive_failures": 2, "incident": "prior"})

    async def once():
        stop.set()
        raise APIError("CONNECTION_NOT_VERIFIED")

    monkeypatch.setattr(worker, "once", once)
    await worker.run(stop)
    assert not any(record.levelno >= 30 for record in caplog.records)
    assert len(r.store.rows("SELECT 1 FROM inbox WHERE kind='worker_failed'")) == 1


async def test_relist_critical_conflict_retains_exact_candidate_and_notification(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-unit")
    r.store.set_setting("notifications:initialized", True)
    r.api.item["status"] = "SOLD"
    r.api.items["candidate"] = {
        **deepcopy(r.api.item),
        "id": "candidate",
        "status": "APPROVED",
        "category": {"id": "different-category"},
    }
    await LogicalProductWorker(r.store, r.api).once()
    product = LogicalProducts(r.store, r.api).listing("lot")
    assert product["error_code"] == "RELIST_CRITICAL_FIELDS_CHANGED"
    event = r.store.rows("SELECT payload FROM inbox WHERE kind='logical_product_blocked'")
    payload = json.loads(event[0]["payload"])
    assert payload["candidate_id"] == "candidate" and payload["revision"] == product["revision"]
    await Notifications(r.store, r.bot).once()
    notices = [
        m
        for m in r.session.calls
        if "параметры" in (getattr(m, "text", "") or "")
        and "Проверьте привязку товара" in (getattr(m, "text", "") or "")
    ]
    assert notices and "item:candidate" in buttons(notices[-1])
    assert "lp:item:lot" not in buttons(notices[-1])
    assert not r.api.messages and not r.api.remote.payments


async def test_unknown_candidate_opens_existing_product_links_not_arbitrary_alias(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-unit")
    r.store.set_setting("notifications:initialized", True)
    product = LogicalProducts(r.store, r.api).listing("lot")
    LogicalProductWorker(r.store, r.api).problem(product, "RELIST_CONFIRMATION_REQUIRED")
    await Notifications(r.store, r.bot).once()
    notices = [m for m in r.session.calls if "Проверьте привязку товара" in (getattr(m, "text", "") or "")]
    route = "lp:product:" + product["id"]
    assert route in buttons(notices[-1]) and "Кандидат неизвестен" in notices[-1].text
    r.api.calls.clear()
    await r.ui.click(route)
    assert "Связанные объявления" in r.session.last_panel.text
    assert product["code"] in r.session.last_panel.text and not r.api.calls
    assert not r.api.messages and not r.api.remote.payments


async def test_periodic_verification_during_scan_does_not_create_permanent_incident(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-unit")
    original, count = r.api.call, 0

    async def call(method, **kwargs):
        nonlocal count
        result = await original(method, **kwargs)
        if method == "get_item":
            count += 1
            if count >= 2:
                r.api.account.writes_enabled = False
                r.api.connection = SimpleNamespace(awaiting_verification=True)
        return result

    r.api.call = call
    with pytest.raises(APIError, match="CONNECTION_NOT_VERIFIED"):
        await LogicalProductWorker(r.store, r.api).once()
    assert not r.store.rows("SELECT 1 FROM inbox WHERE kind='logical_product_blocked'")
    assert LogicalProducts(r.store, r.api).listing("lot")["error_code"] is None
    r.api.call, r.api.account.writes_enabled = original, True
    r.api.connection = None
    await LogicalProductWorker(r.store, r.api).once()
    assert LogicalProducts(r.store, r.api).listing("lot")["error_code"] is None
    assert not r.api.messages and not r.api.remote.payments


async def test_obsolete_candidate_and_resolved_incident_are_not_sent_after_restart(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-unit")
    r.store.set_setting("notifications:initialized", True)
    product = LogicalProducts(r.store, r.api).listing("lot")
    worker = LogicalProductWorker(r.store, r.api)
    worker.problem(product, "RELIST_BINDING_CONFLICT", candidate_id="old-candidate")
    stale = r.store.rows("SELECT key FROM inbox WHERE kind='logical_product_blocked'")[0]["key"]
    r.store.execute("INSERT INTO notifications(event_key,owner_id,status) VALUES(?,7,'PENDING')", (stale,))
    worker.problem(product, "RELIST_BINDING_CONFLICT", candidate_id="current-candidate")
    for _ in range(2):
        await Notifications(r.store, r.bot).once()
    notices = [m for m in r.session.calls if "Проверьте привязку товара" in (getattr(m, "text", "") or "")]
    assert len(notices) == 1 and "item:current-candidate" in buttons(notices[0])
    assert r.store.rows("SELECT status FROM notifications WHERE event_key LIKE '%old-candidate'") == [
        {"status": "SKIPPED"}
    ]
    worker.problem(product, "RELIST_ITEM_CHANGED", candidate_id="unseen")
    r.store.execute("UPDATE logical_products SET error_code=NULL WHERE id=?", (product["id"],))
    await Notifications(r.store, r.bot).once()
    assert (
        len([m for m in r.session.calls if "Проверьте привязку товара" in (getattr(m, "text", "") or "")])
        == 1
    )
    assert not r.api.messages and not r.api.remote.payments


@pytest.mark.parametrize("fault", ["network", "incomplete"])
async def test_temporary_candidate_read_is_not_a_binding_conflict(shop, fault):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-unit")
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_item":
            if fault == "network":
                raise APIError("NETWORK_ERROR")
            return {"id": kwargs["id"], "name": r.api.item["name"]}
        return await original(method, **kwargs)

    r.api.call = call
    with pytest.raises(APIError):
        await LogicalProductWorker(r.store, r.api).once()
    assert not r.store.rows("SELECT 1 FROM inbox WHERE kind='logical_product_blocked'")
    assert LogicalProducts(r.store, r.api).listing("lot")["error_code"] is None
    r.api.call = original
    await LogicalProductWorker(r.store, r.api).once()
    assert LogicalProducts(r.store, r.api).listing("lot")["error_code"] is None
    assert not r.api.messages and not r.api.remote.payments


async def test_stock_rename_recovers_matching_binding_conflict_through_readiness_button(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    logical = LogicalProducts(r.store, r.api)
    before = logical.listing("lot")
    r.api.item["name"] = "Новое название [" + before["code"] + "]"
    worker = LogicalProductWorker(r.store, r.api)
    worker.problem(before, "RELIST_BINDING_CONFLICT", candidate_id="lot")
    await r.ui.click("item:lot")
    assert "rd:check:lot" in buttons(r.session.last_panel)
    await r.ui.click("rd:check:lot")
    after = logical.listing("lot")
    assert after["title_snapshot"] == r.api.item["name"] and after["error_code"] is None
    assert all(after[k] == before[k] for k in ("enabled", "armed_at", "revision", "control_revision"))
    assert not r.api.messages and not r.api.remote.payments


async def test_stock_rename_during_moderation_refreshes_title_without_blocking(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    logical = LogicalProducts(r.store, r.api)
    before = logical.listing("lot")
    r.api.item.update(name="Новое название [" + before["code"] + "]", status="PENDING_MODERATION")
    r.api.call = _catalog_including_moderation(r.api.call, r.api.item)
    await LogicalProductWorker(r.store, r.api).once()
    after = logical.listing("lot")
    assert after["title_snapshot"] == r.api.item["name"] and after["error_code"] is None
    assert after["enabled"] == before["enabled"] == 1


def _catalog_including_moderation(original, item):
    async def call(method, **kwargs):
        if method == "get_my_items":
            return {"items": [deepcopy(item)], "page_info": {"has_next_page": False}}
        return await original(method, **kwargs)

    return call


async def test_paid_candidate_rate_limit_stops_scan_and_retries_same_candidate(shop):
    from app.services.orders import Orders

    r = await shop()
    await setup_ui(r, "fixed_text", "private-unit")
    r.api.item["status"] = "SOLD"
    r.api.items["candidate"] = {**deepcopy(r.api.item), "id": "candidate"}
    r.api.items["first-candidate"] = {**deepcopy(r.api.item), "id": "first-candidate"}
    Orders(r.store).ingest(r.api.order("first-order", "first-candidate"))
    order = r.api.order("rate-limited-order", "candidate")
    Orders(r.store).ingest(order)
    original = r.api.call
    attempts = 0

    async def call(method, **kwargs):
        nonlocal attempts
        if method == "get_item" and kwargs["id"] == "candidate":
            attempts += 1
            if attempts == 1:
                raise APIError("RATE_LIMITED", retry_after="75")
        return await original(method, **kwargs)

    r.api.call = call
    worker = LogicalProductWorker(r.store, r.api)
    with pytest.raises(APIError, match="RATE_LIMITED") as caught:
        await worker.once()
    assert caught.value.retry_after == "75"
    await worker.once()
    assert attempts >= 2 and LogicalProducts(r.store, r.api).listing("candidate")
    assert LogicalProducts(r.store, r.api).listing("first-candidate")
    assert not r.api.messages and not r.api.remote.payments


async def test_stock_marketing_edit_during_scan_does_not_create_pause(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    logical = LogicalProducts(r.store, r.api)
    old = logical.listing("lot")
    original = r.api.call
    reads = 0

    async def call(method, **kwargs):
        nonlocal reads
        if method == "get_item" and kwargs["id"] == "lot":
            reads += 1
            if reads == 2:
                r.api.item["description"] = "Updated marketing description"
                r.api.item["name"] = "Updated name [" + old["code"] + "]"
                r.api.item["comment"] = "Seller comment"
        return await original(method, **kwargs)

    r.api.call = call
    await LogicalProductWorker(r.store, r.api).once()
    after = logical.listing("lot")
    assert after["enabled"] and after["error_code"] is None
    assert after["title_snapshot"] == r.api.item["name"]
    logical.verify_listing(after, r.api.item)
