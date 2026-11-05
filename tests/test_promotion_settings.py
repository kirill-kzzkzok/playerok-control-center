from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, Store, now
from app.services.promotion_settings import PromotionSettings
from app.services.promotions import PromotionPolicy
from app.telegram.promotions_panel import PromotionsPanel
from app.telegram.storage import SQLiteStorage


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "promotion.sqlite")
    store.claim_first_owner(1)
    item = {
        "id": "item",
        "status": "APPROVED",
        "raw_price": 100,
        "priority_position": 20,
        "user": {"id": "seller"},
    }

    async def call(method, **kwargs):
        if method == "get_item":
            return item
        assert method == "get_item_priority_statuses" and kwargs == {"item_id": "item", "item_price": 100}
        return [{"id": "premium", "type": "PREMIUM", "price": 10}]

    api = SimpleNamespace(account=SimpleNamespace(id="seller"), call=AsyncMock(side_effect=call))
    yield store, api, item, PromotionSettings(store, api)
    store.close()


def active(**changes):
    return PromotionPolicy(
        enabled=True, per_operation="20", daily="100", monthly="1000", max_operations_per_item=3, **changes
    )


async def test_policy_persistence_selection_exclusion_owner_and_cas(setup):
    store, api, _, service = setup
    assert service.get() == (0, PromotionPolicy(), "selected")
    service.save(1, 0, asdict(active()), "all")
    assert PromotionSettings(store, api).get()[0] == 1
    assert service.selected("item", "all")
    await service.target(1, "item", "exclude")
    assert not service.selected("item", "all")
    await service.target(1, "item", "include")
    assert service.selected("item", "selected")
    with pytest.raises(Conflict):
        service.save(1, 0, asdict(active()), "all")
    with pytest.raises(Conflict):
        service.save(2, 1, asdict(active()), "all")
    service.stop(1)
    assert not service.get()[1].enabled


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": 1},
        {"check_seconds": 1},
        {"night_interval_seconds": 1},
        {"night_start_hour": 24},
        {"night_start_hour": 8},
        {"per_operation": "NaN"},
        {"threshold": True},
    ],
)
def test_policy_rejects_invalid_bools_intervals_night_and_money(values):
    with pytest.raises(ValueError):
        PromotionPolicy(**values)


def test_configurable_night_hours_and_two_fresh_position_checks():
    at = datetime(2026, 10, 30, 20, 30, tzinfo=UTC)  # 23:30 Moscow
    policy = active(night_enabled=True)
    assert not policy.due(at, at - timedelta(hours=2))
    assert policy.due(at, at - timedelta(hours=4))
    policy = active(mode="position")
    assert policy.due(at, None, [(20, at - timedelta(seconds=120)), (19, at)])
    assert not policy.due(at, None, [(20, at), (19, at)])
    assert not policy.due(at, None, [(True, at - timedelta(seconds=120)), (19, at)])
    assert not policy.due(at, None, [(20, at.replace(tzinfo=None)), (19, at)])


async def test_real_quote_dry_run_never_charges_and_journals_separate_evaluation(setup):
    store, _, _, service = setup
    service.save(1, 0, asdict(active()), "all")
    result = await service.evaluate(1, "item", "premium")
    assert result["would_allow"] and not result["charged"] and result["paid_execution_available"]
    assert len(store.rows("SELECT * FROM promotion_evaluations")) == 1
    assert not store.rows("SELECT * FROM promotion_expenses")
    assert not store.rows("SELECT * FROM outbox")


async def test_position_mode_requires_two_spaced_real_reads(setup):
    store, _, _, service = setup
    service.save(1, 0, asdict(active(mode="position")), "all")
    assert not (await service.evaluate(1, "item", "premium"))["would_allow"]
    assert not (await service.evaluate(1, "item", "premium"))["would_allow"]
    # Previous stored read occurred 121 seconds ago; current read comes from mock API.
    store.execute(
        "UPDATE promotion_observations SET checked_at=?",
        ((datetime.now(UTC) - timedelta(seconds=121)).isoformat(),),
    )
    assert (await service.evaluate(1, "item", "premium"))["would_allow"]


async def test_limits_include_unknown_and_reserved_charges_with_moscow_day_boundary(setup):
    store, _, _, service = setup
    service.save(1, 0, asdict(active()), "all")
    store.execute("INSERT INTO promotion_expenses VALUES('reserved','other','95','RESERVED',?)", (now(),))
    result = await service.evaluate(1, "item", "premium")
    assert not result["would_allow"] and "дневной лимит" in result["reasons"]
    assert not result["policy_auto_disabled"] and service.get()[1].enabled
    assert len(store.rows("SELECT * FROM promotion_expenses")) == 1


async def test_inactive_excluded_and_busy_items_are_not_allowed(setup):
    store, _, item, service = setup
    service.save(1, 0, asdict(active()), "all")
    await service.target(1, "item", "exclude")
    item["status"] = "EXPIRED"
    store.enqueue("edit", "edit", "item", {"price": 100})
    result = await service.evaluate(1, "item", "premium")
    assert not result["would_allow"] and len(result["reasons"]) == 3


async def test_telegram_settings_restart_and_readonly_evaluation(setup):
    store, api, _, service = setup
    panel = PromotionsPanel(store, api)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=1, user_id=1))
    await panel.callback("promotions", 1, state)
    nonce = (await state.get_data())["promotion"]["nonce"]
    await panel.callback(f"pp:{nonce}:edit:daily", 1, state)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=1, user_id=1))
    await panel.message(SimpleNamespace(text="123,45", from_user=SimpleNamespace(id=1)), state)
    assert service.get()[0] == 0
    nonce = (await state.get_data())["promotion"]["nonce"]
    await panel.callback(f"pp:{nonce}:save", 1, state)
    assert service.get()[1].daily == "123.45"
    with pytest.raises(Conflict):
        await panel.callback(f"pp:{nonce}:save", 1, state)
    await panel.callback("promo:item:item", 1, state)
    nonce = (await state.get_data())["promotion_item"]["nonce"]
    text, _ = await panel.callback(f"pi:{nonce}:quote:0", 1, state)
    assert "Списано: 0" in text
    assert not store.rows("SELECT * FROM outbox")
