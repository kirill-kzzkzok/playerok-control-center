import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, Store
from app.services.automation import Automation, DeliveryPolicy
from app.services.orders import Orders
from app.services.products import ValidationError
from app.telegram.automation_panel import AutomationPanel
from app.telegram.storage import SQLiteStorage


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "automation.sqlite")
    store.claim_first_owner(1)
    order = {
        "id": "order",
        "direction": "OUT",
        "status": "PAID",
        "has_problem": False,
        "is_automated": False,
        "item": {"id": "item", "user": {"id": "seller"}},
        "chat": {"id": "chat"},
    }

    async def call(method, **kwargs):
        if method == "get_item":
            return {"id": kwargs["id"], "user": {"id": "seller"}}
        if method == "get_deal":
            return order
        if method == "get_chat":
            return {"id": "chat", "is_texting_allowed": True}
        raise AssertionError(method)

    api = SimpleNamespace(
        account=SimpleNamespace(id="seller", writes_enabled=False), call=AsyncMock(side_effect=call)
    )
    yield store, api, order, Automation(store, api)
    store.close()


async def test_saved_policy_version_owner_and_global_stop(setup):
    store, api, _, service = setup
    values = asdict(
        DeliveryPolicy(enabled=True, mode="text", text="private data", greeting="hello", after="thanks")
    )
    assert await service.save(1, "item", 0, values) == 1
    assert Automation(store, api).get("item")[1].text == "private data"
    with pytest.raises(Conflict):
        await service.save(1, "item", 0, values)
    before = api.call.await_count
    with pytest.raises(Conflict):
        await service.save(2, "item", 1, values)
    assert api.call.await_count == before
    assert service.stop_all(1) == 1
    assert service.get("item")[0] == 2 and not service.get("item")[1].enabled
    assert "private data" not in str(store.rows("SELECT * FROM audit_log"))


async def test_overlapping_policy_edit_scenarios_commit_one_revision(setup):
    store, api, _, service = setup
    original = api.call.side_effect
    both = asyncio.Event()
    reads = 0

    async def call(method, **kwargs):
        nonlocal reads
        result = await original(method, **kwargs)
        if method == "get_item":
            reads += 1
            if reads == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
        return result

    api.call.side_effect = call
    results = await asyncio.gather(
        *[
            service.save(1, "item", 0, asdict(DeliveryPolicy(enabled=True, mode="text", text=text)))
            for text in ["first scenario", "second scenario"]
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(value, Conflict) for value in results) == 1
    assert sum(value == 1 for value in results) == 1
    revision, policy = service.get("item")
    assert revision == 1 and policy.text in {"first scenario", "second scenario"}
    assert not store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": True},
        {"mode": "text", "text": ""},
        {"mode": "future"},
        {"enabled": 1},
        {"greeting": "😀" * 1751},
        {"mark_sent": "true"},
    ],
)
def test_invalid_policy_cannot_be_saved(values):
    with pytest.raises((ValidationError, TypeError)):
        DeliveryPolicy(**values)


async def test_actual_sale_dry_run_never_reserves_or_queues(setup):
    store, _, _, service = setup
    policy = DeliveryPolicy(
        enabled=True, mode="stock", greeting="private greeting", after="private after", mark_sent=True
    )
    await service.save(1, "item", 0, asdict(policy))
    Orders(store).add_stock("item", ["private code"])
    result = await service.simulate(1, "order")
    assert result["would_deliver"] and result["writes"] == 0 and not result["runtime_enabled"]
    assert result["steps"] == [
        "greeting",
        "unique_stock",
        "after",
        "seller_fulfillment_requires_separate_checks",
    ]
    assert not store.rows("SELECT * FROM reservations") and not store.rows("SELECT * FROM outbox")
    assert store.rows("SELECT status FROM inventory") == [{"status": "AVAILABLE"}]
    assert "private" not in str(store.rows("SELECT result FROM automation_runs"))


async def test_dry_run_reports_empty_stock_local_problem_and_prior_delivery(setup):
    store, _, order, service = setup
    await service.save(1, "item", 0, asdict(DeliveryPolicy(enabled=True, mode="stock")))
    Orders(store).ingest(order)
    Orders(store).annotate("order", 1, 0, True, "Needs attention")
    store.enqueue("delivery:order", "delivery", "order", {"inventory_id": 1})
    result = await service.simulate(1, "order")
    assert not result["would_deliver"]
    assert len(result["reasons"]) == 3
    assert not result["steps"]


async def test_policy_form_survives_restart_and_rejects_stale_buttons(setup):
    store, api, _, service = setup
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=1, user_id=1))
    panel = AutomationPanel(store, api)
    await panel.callback("auto:item:item", 1, state)
    nonce = (await state.get_data())["automation"]["nonce"]
    await panel.callback(f"ap:{nonce}:mode:text", 1, state)
    with pytest.raises(Conflict):
        await panel.callback(f"ap:{nonce}:toggle:enabled", 1, state)
    nonce = (await state.get_data())["automation"]["nonce"]
    await panel.callback(f"ap:{nonce}:edit:text", 1, state)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=1, user_id=1))
    panel = AutomationPanel(store, api)
    preview, _ = await panel.message(
        SimpleNamespace(text="private delivery data", from_user=SimpleNamespace(id=1)), state
    )
    assert "private delivery data" not in preview
    assert service.get("item")[0] == 0
    nonce = (await state.get_data())["automation"]["nonce"]
    await panel.callback(f"ap:{nonce}:save", 1, state)
    assert service.get("item")[1].text == "private delivery data"
    assert not store.rows("SELECT * FROM outbox")
    assert json.loads(store.rows("SELECT payload FROM delivery_policies")[0]["payload"])["enabled"] is False
