"""New launch consent, real delivery workers and boundary effects."""

import pytest

from app.database.store import Store
from app.services.automation import Automation
from app.services.products import Products
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.outbox import Executor, OutboxWorker
from tests.test_gifts import rig  # noqa: F401
from tests.test_listing_launch import configured
from tests.test_listing_launch import launch as launch_fixture

launch = launch_fixture


@pytest.mark.parametrize("mode", ["gifts", "text", "stock", "manual"])
async def test_new_launch_defaults_are_automatic_only_for_automated_modes(launch, mode):
    r = launch
    ident = await r.launch.open(7, "item", {"delivery": mode})
    value = r.launch.load(7, ident)["payload"]
    assert value["auto_complete"] is (mode != "manual")
    assert "inherit" not in value  # Relisting inherits automatically; no separate launch choice.
    value["auto_complete"] = False
    draft = r.launch.load(7, ident)
    r.store.update_draft(ident, 7, draft["revision"], value)
    assert await r.launch.open(7, "item") == ident
    assert not r.launch.load(7, ident)["payload"]["auto_complete"]


@pytest.mark.parametrize("mode", ["text", "stock"])
async def test_launch_confirms_delivery_before_one_automatic_completion(launch, mode):
    r = launch
    ident, revision = await configured(r, mode=mode)
    await r.launch.queue(7, ident, revision)
    await r.worker.once()
    for _ in range(12):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert r.api.sent
    assert r.api.orders["immediate"]["status"] == "SENT"
    assert r.store.rows("SELECT status FROM outbox WHERE kind='fulfill'") == [{"status": "SUCCEEDED"}]
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "SUCCEEDED"}]
    assert not await r.worker.once()
    assert not r.remote.payments
    assert len(r.api.refunds) == 1


@pytest.mark.parametrize("failure", ["owner_stop", "report_required", "lost_ack"])
async def test_completion_cannot_override_stop_report_or_unknown(launch, failure):
    r = launch
    ident, revision = await configured(r, mode="text")
    await r.launch.queue(7, ident, revision)
    await r.worker.once()
    for _ in range(8):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        if r.store.rows("SELECT 1 FROM outbox WHERE kind='fulfill' AND status='PENDING'"):
            break
        await r.worker.once()
    assert r.api.sent and not r.api.refunds
    if failure == "owner_stop":
        Automation(r.store, r.api).stop_all(7)
    elif failure == "report_required":
        r.api.orders["immediate"]["item"]["user"]["role"] = "OFFICIAL_SELLER"
        original = r.api.call

        async def call(method, **kwargs):
            if method == "get_fulfillment_requirement":
                return {"requirement": "fields"}
            if method == "get_fulfillment_entry":
                return {"state": "absent"}
            return await original(method, **kwargs)

        r.api.call = call
    else:
        r.api.refund_error = True
    await r.worker.once()
    if failure == "lost_ack":
        path = r.store.path
        r.store.close()
        r.store = Store(path)
        r.worker = OutboxWorker(r.store, Executor(r.store, r.api, Products(r.store, r.api)))
    for _ in range(3):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    operation = r.store.rows("SELECT status FROM outbox WHERE kind='fulfill'")[0]
    assert operation["status"] == ("UNCERTAIN" if failure == "lost_ack" else "PERMANENT_FAILED")
    assert len(r.api.refunds) == (1 if failure == "lost_ack" else 0)
    assert len(r.api.sent) == 1
    assert not r.remote.payments


async def test_old_completion_reminder_does_not_become_automatic_permission(launch):
    r = launch
    ident, revision = await configured(r, mode="text", auto_complete=False)
    await r.launch.queue(7, ident, revision)
    await r.worker.once()
    # Old JSON row, exactly the pre-upgrade shape, is a reminder, not permission.
    r.store.execute(
        "UPDATE delivery_policies SET payload=json_remove(json_set(payload,'$.mark_sent',json('true')),'$.automatic_completion')"
    )
    _, policy = Automation(r.store, r.api).get("item")
    assert policy.mark_sent and not policy.automatic_completion
    for _ in range(8):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert len(r.api.sent) == 1 and not r.api.refunds
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "NEEDS_FULFILLMENT"}]
