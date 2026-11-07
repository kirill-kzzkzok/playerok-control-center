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
        and "Проверьте постоянный код" in (getattr(m, "text", "") or "")
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
    notices = [m for m in r.session.calls if "Проверьте постоянный код" in (getattr(m, "text", "") or "")]
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
    notices = [m for m in r.session.calls if "Проверьте постоянный код" in (getattr(m, "text", "") or "")]
    assert len(notices) == 1 and "item:current-candidate" in buttons(notices[0])
    assert r.store.rows("SELECT status FROM notifications WHERE event_key LIKE '%old-candidate'") == [
        {"status": "SKIPPED"}
    ]
    worker.problem(product, "RELIST_ITEM_CHANGED", candidate_id="unseen")
    r.store.execute("UPDATE logical_products SET error_code=NULL WHERE id=?", (product["id"],))
    await Notifications(r.store, r.bot).once()
    assert (
        len([m for m in r.session.calls if "Проверьте постоянный код" in (getattr(m, "text", "") or "")]) == 1
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
