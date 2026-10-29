from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from PIL import Image

from app.database.store import Store
from app.rendering.cards import profile_card
from app.rendering.charts import revenue_chart
from app.security.access import OwnerMiddleware
from app.services.orders import Orders
from app.services.products import Products, ValidationError
from app.services.promotions import PromotionPolicy, dry_run
from app.services.statistics import statistics
from app.telegram.storage import SQLiteStorage
from app.telegram.wizard import Wizard


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "ui.sqlite")
    yield value
    value.close()


def test_only_one_first_owner_across_connections(store):
    def claim(ident):
        connection = Store(store.path)
        try:
            return ident, connection.claim_first_owner(ident)
        finally:
            connection.close()

    with ThreadPoolExecutor(4) as executor:
        results = list(executor.map(claim, range(100, 110)))
    assert sum(bool(ok) for _, ok in results) == 1
    winner = next(ident for ident, ok in results if ok)
    assert store.claim_first_owner(winner)
    assert not store.claim_first_owner(999)


async def test_owner_middleware_private_and_persistent(store):
    gate = OwnerMiddleware([], store, first_start_claim=True)
    handler = AsyncMock()

    def event(ident, kind="private", text="/start"):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=ident), chat=SimpleNamespace(type=kind), text=text
        )

    await gate(handler, event(1, "group"), {})
    handler.assert_not_called()
    await gate(handler, event(1, text="hello"), {})
    handler.assert_not_called()
    await gate(handler, event(1), {})
    assert handler.await_count == 1
    await gate(handler, event(2), {})
    assert handler.await_count == 1
    await OwnerMiddleware([], store, first_start_claim=False)(handler, event(1, text="anything"), {})
    assert handler.await_count == 2


def test_profile_and_chart_render_real_values():
    image = profile_card({"username": "<name>", "rating": 4.8, "reviews_count": 12})
    with Image.open(BytesIO(image)) as img:
        assert img.size == (1100, 480)
    chart = revenue_chart({"daily": {"2026-10-29": 100}, "categories": {"Steam": 100}})
    with Image.open(BytesIO(chart)) as img:
        assert img.width > 1000


def test_statistics_excludes_refunds_from_revenue_and_future(store):
    service = Orders(store)
    for ident, status, when in [
        ("one", "CONFIRMED", "2026-10-29T10:00:00+00:00"),
        ("two", "ROLLED_BACK", "2026-10-29T10:00:00+00:00"),
        ("future", "PAID", "2027-01-01T00:00:00+00:00"),
    ]:
        service.ingest(
            {
                "id": ident,
                "status": status,
                "direction": "OUT",
                "item": {"id": "p", "price": 100},
                "created_at": when,
            }
        )
    stats = statistics(store, 1, datetime(2026, 10, 29, 12, tzinfo=UTC))
    assert stats["orders"] == 2
    assert stats["revenue"] == 100
    assert stats["refunds"] == 100
    assert stats["confirmed_revenue"] == 100
    assert stats["daily"]["2026-10-29"] == 100


def test_promotion_defaults_and_two_position_checks(store):
    at = datetime.now(UTC)
    policy = PromotionPolicy(enabled=True, mode="position")
    assert not policy.due(at, None, [(11, at)])
    assert policy.due(at, None, [(11, at - timedelta(seconds=120)), (12, at)])
    assert not policy.due(at, at - timedelta(seconds=20), [(11, at - timedelta(seconds=120)), (12, at)])
    assert not PromotionPolicy().due(at, None)
    result = dry_run(store, "p", {"price": 10, "checked_at": at.isoformat()}, policy, at)
    assert not result["would_allow"] and not result["charged"]
    assert not store.rows("SELECT * FROM promotion_expenses")


class FakeAPI:
    account = SimpleNamespace(writes_enabled=False)

    async def call(self, method, **kwargs):
        if method == "get_games":
            return {"games": [{"id": "g1", "name": "Test"}], "page_info": {}}
        if method == "get_game":
            return {"categories": [{"id": "c1", "name": "Category"}]}
        if method == "get_game_category":
            return {"id": kwargs["id"], "options": []}
        raise AssertionError(method)

    async def pages(self, method, collection, **kwargs):
        if method == "get_game_category_obtaining_types":
            yield [{"id": "o1", "name": "Manual"}]
        elif method == "get_game_category_agreements":
            yield []
        elif method == "get_game_category_data_fields":
            yield [
                {
                    "id": "secret",
                    "label": "Data",
                    "required": True,
                    "type": "ITEM_DATA",
                    "input_type": "TEXTAREA",
                }
            ]
        else:
            raise AssertionError(method)


async def test_wizard_full_local_flow_restart_and_stale_callback(store):
    api = FakeAPI()
    wizard = Wizard(store, api, Products(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=2, user_id=2))
    await wizard.start(2, state)
    first = (await state.get_data())["view_token"]
    for _ in range(3):
        nonce = (await state.get_data())["view_token"]
        await wizard.callback(2, state, f"w:{nonce}:choose:0")
    with pytest.raises(ValidationError):
        await wizard.callback(2, state, f"w:{first}:choose:0")
    # Durable FSM can be reopened without an in-memory state machine.
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=2, user_id=2))

    async def text(value):
        return await wizard.message(SimpleNamespace(from_user=SimpleNamespace(id=2), text=value), state)

    await text("seller data")
    await text("<Product 1>")
    await text("Description")
    nonce = (await state.get_data())["view_token"]
    await wizard.callback(2, state, f"w:{nonce}:photos_done")
    await text("100")
    nonce = (await state.get_data())["view_token"]
    await wizard.callback(2, state, f"w:{nonce}:choose:0")
    nonce = (await state.get_data())["view_token"]
    text_result, _ = await wizard.callback(2, state, f"w:{nonce}:confirm_done")
    assert "&lt;Product 1&gt;" in text_result
    assert "seller data" not in text_result
    assert len(store.rows("SELECT * FROM drafts")) == 1
    assert not store.rows("SELECT * FROM outbox")
