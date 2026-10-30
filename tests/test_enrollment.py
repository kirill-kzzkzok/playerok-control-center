from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.store import Store, now
from app.playerok.adapter import APIError
from app.services.automation import Automation, DeliveryPolicy, future_order
from app.services.orders import Orders
from app.workers.enrollment import EnrollmentWorker
from app.workers.notifications import Notifications


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "enroll.sqlite")
    store.claim_first_owner(1)
    store.set_setting("orders:last_complete_sync", now())
    orders = {}

    async def call(method, **kwargs):
        if method == "get_item":
            return {"id": "item", "user": {"id": "seller"}}
        if method == "get_deal":
            return deepcopy(orders[kwargs["deal_id"]])
        if method == "get_chat":
            return {"id": "chat", "is_texting_allowed": True}
        raise AssertionError(method)

    api = SimpleNamespace(
        account=SimpleNamespace(id="seller", writes_enabled=True, automation_enabled=True),
        call=AsyncMock(side_effect=call),
    )
    yield store, api, orders, Automation(store, api), EnrollmentWorker(store, api)
    store.close()


async def arm(service, *, confirmed=True):
    return await service.save(
        1,
        "item",
        0,
        asdict(DeliveryPolicy(enabled=True, mode="text", text="private code", auto_enroll=True)),
        future_confirmed=confirmed,
    )


def order(store, orders, ident, created_at):
    value = {
        "id": ident,
        "status": "PAID",
        "direction": "OUT",
        "has_problem": False,
        "is_automated": False,
        "created_at": created_at,
        "item": {"id": "item", "user": {"id": "seller"}},
        "chat": {"id": "chat"},
    }
    orders[ident] = value
    Orders(store).ingest(value)


async def test_only_future_order_enrolled_and_duplicates_do_not_create_new_chain(setup):
    store, _api, orders, service, worker = setup
    order(store, orders, "historical", "2020-01-01T00:00:00Z")
    await arm(service)
    order(store, orders, "future", now())
    assert await worker.once() == 1
    assert await worker.once() == 0
    assert store.rows("SELECT order_id FROM delivery_chains") == [{"order_id": "future"}]
    assert store.rows("SELECT status FROM auto_enrollment WHERE order_id='historical'") == [
        {"status": "SKIPPED"}
    ]
    assert not store.rows("SELECT * FROM outbox")  # enrollment only; separate chain scheduler advances
    assert "private code" not in str(store.rows("SELECT * FROM auto_enrollment"))


@pytest.mark.parametrize(
    "date", [None, "invalid", "2026-10-30T01:00:00", "2100-01-01T00:00:00Z", "2020-01-01T00:00:00Z"]
)
async def test_missing_naive_future_or_old_creation_time_never_uses_local_receive_time(setup, date):
    store, api, orders, service, worker = setup
    await arm(service)
    order(store, orders, "bad", date)
    before = api.call.await_count
    assert await worker.once() == 0
    assert api.call.await_count == before
    assert not store.rows("SELECT * FROM delivery_chains")


async def test_global_disabled_or_baseline_missing_makes_no_api_requests(setup):
    store, api, orders, service, worker = setup
    await arm(service)
    order(store, orders, "new", now())
    api.call.reset_mock()
    api.account.automation_enabled = False
    assert await worker.once() == 0
    api.account.automation_enabled = True
    store.set_setting("orders:last_complete_sync", None)
    assert await worker.once() == 0
    api.call.assert_not_called()
    assert not store.rows("SELECT * FROM auto_enrollment")


async def test_explicit_arm_confirmation_required_and_old_default_does_not_opt_in(setup):
    store, _, orders, service, worker = setup
    with pytest.raises(ValueError, match="отдельно"):
        await arm(service, confirmed=False)
    assert not store.rows("SELECT * FROM delivery_policies")
    await service.save(1, "item", 0, asdict(DeliveryPolicy(enabled=True, mode="text", text="private code")))
    order(store, orders, "new", now())
    assert await worker.once() == 0
    assert not store.rows("SELECT * FROM auto_enrollment")


async def test_retry_read_after_restart_does_not_repeat_chain_registration(setup):
    store, api, orders, service, worker = setup
    await arm(service)
    order(store, orders, "new", now())
    original = api.call.side_effect

    async def bad(method, **kwargs):
        if method == "get_deal":
            raise APIError("RATE_LIMITED", retry_after="120")
        return await original(method, **kwargs)

    api.call.side_effect = bad
    await worker.once()
    assert store.rows("SELECT status FROM auto_enrollment")[0]["status"] == "PENDING"
    before = api.call.await_count
    assert await EnrollmentWorker(store, api).once() == 0
    assert api.call.await_count == before
    store.execute("UPDATE auto_enrollment SET due_at=0")
    api.call.side_effect = original
    await EnrollmentWorker(store, api).once()
    assert len(store.rows("SELECT * FROM delivery_chains")) == 1
    # Simulate crash after chain transaction, before enrollment completion.
    store.execute("UPDATE auto_enrollment SET status='PENDING',due_at=0")
    api.call.reset_mock()
    await EnrollmentWorker(store, api).once()
    api.call.assert_not_called()
    assert len(store.rows("SELECT * FROM delivery_chains")) == 1


async def test_changed_policy_blocks_claimed_candidate_and_requires_owner_review(setup):
    store, _api, orders, service, worker = setup
    await arm(service)
    order(store, orders, "new", now())
    worker.discover()
    service.stop_all(1)
    await worker.once()
    assert not store.rows("SELECT * FROM delivery_chains")
    assert store.rows("SELECT status FROM auto_enrollment")[0]["status"] == "BLOCKED"
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)))
    await Notifications(store, bot).once()
    await Notifications(store, bot).once()
    assert bot.send_message.await_count == 1


async def test_fresh_api_timestamp_is_rechecked_inside_chain_registration(setup):
    store, _, orders, service, worker = setup
    await arm(service)
    order(store, orders, "new", now())
    orders["new"]["created_at"] = "2020-01-01T00:00:00Z"
    await worker.once()
    assert not store.rows("SELECT * FROM delivery_chains")
    assert store.rows("SELECT status FROM auto_enrollment")[0]["status"] == "BLOCKED"


def test_datetime_comparison_honors_timezone_not_lexicographic_order():
    assert future_order("2020-01-01T03:00:01+03:00", "2020-01-01T00:00:00+00:00")
    assert not future_order("2020-01-01T03:00:00+03:00", "2020-01-01T00:00:01+00:00")


async def test_leasing_prevents_duplicate_processing_and_recovers_after_expiry(setup):
    store, _, orders, service, worker = setup
    await arm(service)
    order(store, orders, "new", now())
    worker.discover()
    first = worker.claim()
    assert first and worker.claim() is None
    store.execute("UPDATE auto_enrollment SET lease_until=0")
    second = worker.claim()
    assert second and second["lease_token"] != first["lease_token"]


async def test_last_stock_is_reserved_once_and_following_sale_is_blocked(setup):
    store, _, orders, service, worker = setup
    await service.save(
        1,
        "item",
        0,
        asdict(DeliveryPolicy(enabled=True, mode="stock", auto_enroll=True)),
        future_confirmed=True,
    )
    Orders(store).add_stock("item", ["one private code"])
    order(store, orders, "one", now())
    order(store, orders, "two", now())
    await worker.once()
    assert len(store.rows("SELECT * FROM reservations")) == 1
    assert len(store.rows("SELECT * FROM delivery_chains")) == 1
    assert store.rows("SELECT status FROM auto_enrollment WHERE order_id='two'") == [{"status": "BLOCKED"}]
    assert not store.setting("delivery:item:enabled")
    assert "private code" not in str(store.rows("SELECT * FROM auto_enrollment"))
