import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.store import Store
from app.playerok.adapter import APIError
from app.services.promotion_settings import PromotionSettings
from app.services.promotions import PromotionPolicy
from app.workers.notifications import Notifications
from app.workers.promotions import PromotionWorker


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "schedule.sqlite")
    store.claim_first_owner(1)

    async def call(method, **kwargs):
        if method == "get_my_items":
            return {"items": [{"id": "item"}], "page_info": {"has_next_page": False}}
        if method == "get_item":
            return {
                "id": kwargs["id"],
                "user": {"id": "seller"},
                "status": "APPROVED",
                "raw_price": 100,
                "priority": "DEFAULT",
                "priority_position": 20,
            }
        if method == "get_item_priority_statuses":
            return [{"id": "standard", "type": "DEFAULT", "price": 0}]
        raise AssertionError(method)

    api = SimpleNamespace(account=SimpleNamespace(id="seller"), call=AsyncMock(side_effect=call))
    yield store, api, PromotionSettings(store, api), PromotionWorker(store, api)
    store.close()


def enable(settings, selection="all", **change):
    policy = PromotionPolicy(enabled=True, max_operations_per_item=10, **change)
    settings.save(1, 0, asdict(policy), selection)


async def test_disabled_policy_does_not_make_api_calls(setup):
    store, api, _, worker = setup
    assert await worker.once() == 0
    api.call.assert_not_called()
    assert store.setting("promotions:health")["status"] == "DISABLED"


async def test_schedule_persists_and_does_not_repeat_before_due(setup):
    store, api, settings, worker = setup
    enable(settings)
    assert await worker.once() == 1
    calls = api.call.await_count
    assert await PromotionWorker(store, api).once() == 0
    assert api.call.await_count == calls
    assert not store.rows("SELECT * FROM outbox")
    assert not store.rows("SELECT * FROM promotion_expenses")
    assert len(store.rows("SELECT * FROM promotion_evaluations")) == 1


async def test_only_explicit_targets_evaluated_and_exclusions_remain_skipped(setup):
    store, api, settings, worker = setup
    enable(settings, "selected")
    assert await worker.once() == 0
    await settings.target(1, "item", "include")
    assert await worker.once() == 1
    await settings.target(1, "item", "exclude")
    store.execute("UPDATE promotion_schedule SET due_at=0")
    calls = api.call.await_count
    assert await worker.once() == 0  # Excluded rows no longer consume evaluator capacity.
    assert api.call.await_count == calls


async def test_discovery_resumes_next_page_after_restart(setup):
    store, api, settings, worker = setup
    enable(settings)
    original = api.call.side_effect

    async def call(method, **kwargs):
        if method == "get_my_items":
            if kwargs["after_cursor"] is None:
                return {
                    "items": [{"id": "one"}],
                    "page_info": {"has_next_page": True, "end_cursor": "cursor"},
                }
            assert kwargs["after_cursor"] == "cursor"
            return {"items": [{"id": "two"}], "page_info": {"has_next_page": False}}
        return await original(method, **kwargs)

    api.call.side_effect = call
    await worker.once()
    await PromotionWorker(store, api).once()
    assert {row["item_id"] for row in store.rows("SELECT item_id FROM promotion_schedule")} == {"one", "two"}


async def test_lease_prevents_parallel_claim_and_expiry_allows_safe_read_recheck(setup):
    store, _, settings, worker = setup
    enable(settings)  # Claims now enforce the real policy/selection admission.
    store.execute("INSERT INTO promotion_schedule(item_id) VALUES('item')")
    first = worker.claim()
    assert first and worker.claim() is None
    store.execute("UPDATE promotion_schedule SET lease_until=0")
    second = worker.claim()
    assert second and second[1] != first[1]


async def test_failed_item_backoff_does_not_prevent_next_item(setup):
    store, api, settings, worker = setup
    enable(settings, "selected")
    await settings.target(1, "bad", "include")
    await settings.target(1, "good", "include")
    original = api.call.side_effect

    async def call(method, **kwargs):
        if method == "get_item" and kwargs["id"] == "bad":
            raise APIError("HTTP_403")
        return await original(method, **kwargs)

    api.call.side_effect = call
    assert await worker.once() == 2
    assert store.rows("SELECT error_code FROM promotion_schedule WHERE item_id='bad'") == [
        {"error_code": "HTTP_403"}
    ]
    assert len(store.rows("SELECT * FROM promotion_evaluations")) == 1


async def test_budget_guard_notification_is_durable_and_not_baselined(setup):
    store, api, settings, worker = setup
    enable(settings)
    original = api.call.side_effect

    async def call(method, **kwargs):
        if method == "get_item_priority_statuses":
            return [{"id": "standard", "type": "DEFAULT", "price": 10}]
        return await original(method, **kwargs)

    api.call.side_effect = call
    await worker.once()
    assert settings.get()[1].enabled
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)))
    await Notifications(store, bot).once()
    await Notifications(store, bot).once()
    assert bot.send_message.await_count == 1
    summary = bot.send_message.call_args.args[1]
    assert "Автоподнятие" in summary
    guard = store.rows("SELECT payload FROM inbox WHERE kind='promotion_guard'")[0]
    import json

    for reason in json.loads(guard["payload"])["reasons"]:
        assert reason in summary
    assert not store.rows("SELECT * FROM promotion_expenses")


async def test_worker_stop_is_cancellable_without_pending_requests(setup):
    _, _, _, worker = setup
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, 1)


async def test_policy_revision_rechecks_before_previous_due_time(setup):
    store, _, settings, worker = setup
    enable(settings)
    await worker.once()
    revision, policy, selection = settings.get()
    settings.save(1, revision, asdict(policy), selection)
    assert await worker.once() == 1
    assert len(store.rows("SELECT * FROM promotion_evaluations")) == 2


async def test_failed_discovery_keeps_cursor_and_respects_backoff(setup):
    store, api, settings, worker = setup
    enable(settings)
    store.set_setting("promotions:discovery", {"revision": 1, "after": "saved", "seen": ["saved"]})
    api.call.side_effect = APIError("RATE_LIMITED", retry_after="300")
    assert await worker.once() == 0
    assert await worker.once() == 0
    assert api.call.await_count == 1
    assert store.setting("promotions:discovery")["after"] == "saved"
