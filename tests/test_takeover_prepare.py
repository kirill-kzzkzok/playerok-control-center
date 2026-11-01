"""Adversarial tests of real PREPARE/COMMIT, never live transports."""

import json
from dataclasses import asdict

import pytest

from app.database.store import Conflict, Store, now
from app.gifts.core import GiftValidationError
from app.services.products import ValidationError
from app.services.promotion_settings import PromotionSettings
from tests.test_gifts import rig as rig_fixture
from tests.test_listing_launch import configured
from tests.test_listing_launch import launch as launch_fixture


@pytest.fixture(name="rig")
def _rig(tmp_path):
    yield from rig_fixture.__wrapped__(tmp_path)


@pytest.fixture(name="launch")
def _launch(rig):
    return launch_fixture.__wrapped__(rig)


BUDGET = {"per_operation": 50, "daily": 100, "monthly": 1000, "max_operations_per_item": 20}


@pytest.mark.parametrize("failure", ["preview", "enqueue"])
async def test_failed_launch_never_enables_global_schedule(launch, monkeypatch, failure):
    r = launch
    ident, revision = await configured(
        r, "text", publication="PREMIUM", quote_id="premium", promotion=True, promotion_policy=BUDGET
    )
    before = r.store.rows("SELECT * FROM promotion_policy")
    original = r.api.call

    async def fail_preview(method, **kwargs):
        if method == "get_item_priority_statuses":
            fail_preview.calls += 1
            if fail_preview.calls == 2:
                raise RuntimeError("injected read failure")
        return await original(method, **kwargs)

    fail_preview.calls = 0
    if failure == "preview":
        r.api.call = fail_preview
    else:

        def fail_enqueue(*args, **kwargs):
            raise RuntimeError("injected COMMIT failure")

        monkeypatch.setattr(Store, "enqueue", fail_enqueue)
    with pytest.raises(RuntimeError):
        await r.launch.queue(7, ident, revision)
    assert r.store.rows("SELECT * FROM promotion_policy") == before
    assert r.launch.load(7, ident)["state"] == "DRAFT"
    assert not r.store.rows("SELECT * FROM outbox")
    assert not r.store.rows("SELECT * FROM delivery_policies")


async def test_launch_preserves_emergency_stop(launch):
    r = launch
    ident, revision = await configured(r)
    r.service.stop(7)
    with pytest.raises((Conflict, ValidationError, GiftValidationError), match="останов"):
        await r.launch.queue(7, ident, revision)
    assert r.store.setting("gifts:stopped") is True
    assert not r.store.rows("SELECT * FROM outbox")


async def test_optional_schema_field_still_requires_exact_order_value(launch):
    r = launch
    r.api.field_required = False
    r.store.set_setting("gifts:stopped", False)  # explicit isolated owner permission
    ident, revision = await configured(r)
    await r.launch.queue(7, ident, revision)
    await r.worker.once()
    assert r.service.discover() == 1
    r.api.orders["immediate"]["obtaining_fields"] = []
    await r.worker.once()
    assert not r.remote.payments
    assert r.service.job("immediate")["state"] != "SUCCEEDED"


@pytest.mark.parametrize("reservation", ["RESERVED", "UNCERTAIN", "SUCCEEDED"])
async def test_premium_commit_obeys_other_orders_and_budget(launch, reservation):
    r = launch
    ident, revision = await configured(
        r, "manual", publication="PREMIUM", quote_id="premium", promotion_policy={**BUDGET, "daily": 10}
    )
    r.store.execute(
        "INSERT INTO promotion_expenses VALUES(?,?,?, ?,?)",
        ("other-reservation", "other-item", "10", reservation, now()),
    )
    with pytest.raises((Conflict, ValidationError)):
        await r.launch.queue(7, ident, revision)
    assert r.launch.load(7, ident)["state"] == "DRAFT"
    assert not r.store.rows("SELECT * FROM outbox")


async def test_premium_commit_detects_policy_changed_after_prepare(launch):
    r = launch
    ident, revision = await configured(
        r, "manual", publication="PREMIUM", quote_id="premium", promotion_policy=BUDGET
    )
    original = r.api.call

    async def changed(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_item_priority_statuses":
            changed.calls += 1
            if changed.calls == 2:
                ps = PromotionSettings(r.store, r.api)
                v, p, selection = ps.get()
                ps.save(7, v, {**asdict(p), "daily": 1}, selection)
        return result

    changed.calls = 0
    r.api.call = changed
    with pytest.raises((Conflict, ValidationError)):
        await r.launch.queue(7, ident, revision)
    assert not r.store.rows("SELECT * FROM outbox")
    assert json.loads(r.store.rows("SELECT payload FROM promotion_policy")[0]["payload"])["daily"] == 1


async def test_text_launch_cannot_overwrite_enabled_gift_binding(launch):
    r = launch
    await r.service.bind(7, "item", "heart")
    await r.service.arm(7, "item", r.service.listing("item")["revision"], prepublication=True)
    other, revision = await configured(r, "text")
    with pytest.raises((Conflict, ValidationError)):
        await r.launch.queue(7, other, revision)
    assert not r.store.rows("SELECT * FROM delivery_policies")


async def test_concurrent_delivery_stop_during_prepare_is_preserved(launch):
    r = launch
    ident, revision = await configured(r, "text")
    original = r.api.call

    async def stopped(method, **kwargs):
        value = await original(method, **kwargs)
        if method == "get_item_priority_statuses":
            r.store.set_setting("delivery:item:enabled", False)
        return value

    r.api.call = stopped
    with pytest.raises((Conflict, ValidationError)):
        await r.launch.queue(7, ident, revision)
    assert r.store.setting("delivery:item:enabled") is False
    assert not r.store.rows("SELECT * FROM outbox")


async def test_enabling_one_published_gift_cannot_resume_global_stop(launch):
    r = launch
    r.api.item["status"] = "APPROVED"
    await r.service.bind(7, "item", "heart")
    r.service.stop(7)
    with pytest.raises((Conflict, ValidationError, GiftValidationError), match="останов"):
        await r.service.arm(7, "item", r.service.listing("item")["revision"])
    assert r.store.setting("gifts:stopped") is True
    assert not r.service.listing("item")["enabled"]
