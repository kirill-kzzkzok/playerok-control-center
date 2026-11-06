"""Upgrade recovery of positively unstarted edits, never replay unknown sends."""

import asyncio

import pytest

from app.database.store import Store
from app.services.logical_products import LogicalProducts
from app.services.products import Products
from app.workers.outbox import Executor, OutboxWorker
from tests.test_logical_faults import reopen
from tests.test_logical_products import logical as logical_fixture
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_ux_redesign import buttons

logical = logical_fixture
shop = shop_fixture


def old_failure(store, ident):
    action = store.claim(operation_id=ident)
    store.finish(ident, action["lease_token"], "PERMANENT_FAILED", error="HTTP_500")


async def test_dispatcher_confirmed_old_edit_recovers_after_restart_once(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "delivery")
    await r.ui.click("editname:lot")
    expected = "Updated " + r.api.item["name"]
    await r.ui.message(expected)
    await r.ui.click(next(key for key in buttons(r.session.last_panel) if key.startswith("commit:")))
    ident = r.store.rows("SELECT id FROM outbox WHERE kind='edit' ORDER BY rowid DESC LIMIT 1")[0]["id"]
    old_failure(r.store, ident)
    before = len(r.api.mutations)
    reopen(r)
    await r.worker.once(operation_id=ident)
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "SUCCEEDED"}]
    assert r.api.item["name"] == expected
    reopen(r)
    await r.worker.once(operation_id=ident)
    assert len(r.api.mutations) == before + 1


async def binding_request(logical):
    store, api, binding = logical
    request = binding.commit(7, await binding.prepare(7, "lot", "fixed_text", {"text": "private unit"}))
    old_failure(store, request["operation_id"])
    return store, api, request["operation_id"]


async def test_old_binding_recovers_linked_receipt_without_arming(logical):
    store, api, ident = await binding_request(logical)
    path = store.path
    store.close()
    restored = Store(path)
    try:
        worker = OutboxWorker(restored, Executor(restored, api, Products(restored, api)))
        await worker.once(operation_id=ident)
        assert restored.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "SUCCEEDED"}]
        product = LogicalProducts(restored, api).listing("lot")
        assert product["state"] == "VERIFIED" and product["verified_at"] and not product["enabled"]
        assert not restored.rows("SELECT * FROM delivery_policies")
        await worker.once(operation_id=ident)
        assert len(api.mutations) == 1
    finally:
        restored.close()


@pytest.mark.parametrize(
    "obstacle",
    [
        "started",
        "late",
        "intent",
        "unknown",
        "no_proof",
        "stop",
        "global_stop",
        "newer",
        "result",
        "payment",
        "no_owner",
        "closed",
    ],
)
async def test_historical_recovery_keeps_unsafe_or_superseded_operation_closed(logical, obstacle):
    store, api, ident = await binding_request(logical)
    if obstacle == "started":
        store.execute("UPDATE outbox SET started_at='2026-11-06' WHERE id=?", (ident,))
    elif obstacle == "late":
        store.record_late_result(ident, "old-worker", "SUCCEEDED", {"remote_id": "lot"})
    elif obstacle == "intent":
        store.set_setting("promotions:intent:" + ident, {"started": True})
    elif obstacle == "unknown":
        store.execute("UPDATE outbox SET status='UNCERTAIN' WHERE id=?", (ident,))
    elif obstacle == "no_proof":
        store.execute("DELETE FROM audit_log WHERE target=? AND action='DISPATCH_OUTCOME'", (ident,))
    elif obstacle == "stop":
        LogicalProducts(store, api).stop(7, LogicalProducts(store, api).listing("lot")["id"])
    elif obstacle == "global_stop":
        store.audit(7, "STOP_ALL_DELIVERY_POLICIES", "*", "0")
    elif obstacle == "newer":
        store.enqueue("newer-edit", "edit", "lot", {"name": "replacement"})
    elif obstacle == "result":
        store.execute("UPDATE outbox SET result='{}' WHERE id=?", (ident,))
    elif obstacle == "payment":
        store.execute("UPDATE outbox SET kind='premium_bump' WHERE id=?", (ident,))
    elif obstacle == "no_owner":
        store.execute("DELETE FROM operation_receipts WHERE operation_id=?", (ident,))
    elif obstacle == "closed":
        store.execute("UPDATE outbox SET error_code='MANUALLY_STOPPED_UNCERTAIN' WHERE id=?", (ident,))
    previous = store.rows("SELECT status FROM outbox WHERE id=?", (ident,))
    worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
    await worker.once(operation_id=ident)
    assert store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == previous
    assert not api.mutations


async def test_recovered_edit_rechecks_server_conflict_before_dispatch(logical):
    store, api, ident = await binding_request(logical)
    api.item["name"] = "Externally changed"
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once(operation_id=ident)
    assert not api.mutations
    assert store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "PERMANENT_FAILED"}]
    assert store.rows("SELECT * FROM audit_log WHERE action='RECOVER_UNSTARTED_EDIT'")
    # A failed revalidation cannot be resurrected every cycle.
    assert store.recover() == 0


@pytest.mark.parametrize("obstacle", ["stop", "late", "newer"])
async def test_recovered_binding_rechecks_changes_during_server_read(logical, obstacle):
    store, api, ident = await binding_request(logical)
    original = api.call

    async def interrupted(method, **kwargs):
        result = await original(method, **kwargs)
        if obstacle == "stop":
            binding = LogicalProducts(store, api)
            binding.stop(7, binding.listing("lot")["id"])
        elif obstacle == "late":
            store.record_late_result(ident, "obsolete-lease", "SUCCEEDED", {"remote_id": "lot"})
        else:
            store.enqueue("newer-edit", "edit", "lot", {"name": "replacement"})
        return result

    api.call = interrupted
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once(operation_id=ident)
    assert not api.mutations


async def test_two_workers_recover_one_original_edit_once(logical):
    store, api, ident = await binding_request(logical)
    workers = [OutboxWorker(store, Executor(store, api, Products(store, api))) for _ in range(2)]
    await asyncio.gather(*(worker.once(operation_id=ident) for worker in workers))
    assert len(api.mutations) == 1
    assert store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "SUCCEEDED"}]
    assert len(store.rows("SELECT * FROM audit_log WHERE action='RECOVER_UNSTARTED_EDIT'")) == 1


async def test_platform_settings_finish_after_old_confirmed_binding_recovery(shop):
    r = await shop()
    await r.ui.click("iv:delivery:lot")
    if "platform:item:lot" not in buttons(r.session.last_panel):
        await click(r, action="lp:method:lot")
    await click(r, action="platform:item:lot")
    await click(r, suffix=":reply")
    await click(r, suffix=":fulfill")
    await click(r, suffix=":save")
    await click(r, suffix=":bind")
    ident = r.store.rows("SELECT id FROM outbox WHERE kind='edit' ORDER BY rowid DESC LIMIT 1")[0]["id"]
    old_failure(r.store, ident)
    reopen(r)
    await r.worker.once(operation_id=ident)
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "SUCCEEDED"}]
    from app.services.automation import Automation

    _, policy = Automation(r.store, r.api).get("lot")
    assert policy.enabled and policy.mode == "playerok" and policy.mark_sent and policy.paid_reply
    assert LogicalProducts(r.store, r.api).listing("lot")["state"] == "VERIFIED"
    assert not r.api.messages and not r.api.remote.payments
