import json
from types import SimpleNamespace

import httpx
import pytest
from aiogram import Bot

from app.database.store import Store
from app.playerok.adapter import Playerok, SafeAccount
from app.services.item_filters import checked_filters, matches_item
from app.services.products import ValidationError
from app.telegram.listings_panel import ListingsPanel
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher


@pytest.mark.parametrize(
    "value",
    [
        {"minimum": "NaN"},
        {"minimum": "-1"},
        {"minimum": 20, "maximum": 10},
        {"category_id": "bad category"},
        {"category_id": ""},
        {"priority": "VIP"},
        {"search": "x" * 201},
        {"extra": True},
    ],
)
def test_invalid_filters_rejected(value):
    with pytest.raises(ValueError):
        checked_filters(value)


def test_matches_exact_price_bounds_priority_and_unicode_literal_name():
    filters = checked_filters(
        {"search": "  ТОВАР_%  ", "minimum": "100", "maximum": "200", "priority": "PREMIUM"}
    )
    item = {"name": "Мой товар_%", "price": "100", "priority": "PREMIUM"}
    assert matches_item(item, filters)
    assert matches_item({**item, "price": 200}, filters)
    for change in [
        {"price": None},
        {"price": "NaN"},
        {"price": 99.99},
        {"price": 200.01},
        {"priority": None},
        {"priority": "DEFAULT"},
        {"name": "товарAB"},
    ]:
        assert not matches_item({**item, **change}, filters)
    assert matches_item({"price": None}, checked_filters({}))
    assert matches_item({"price": 0}, checked_filters({"maximum": 0}))
    assert not matches_item({"priority": None}, checked_filters({"priority": "DEFAULT"}))


async def test_dispatcher_filters_compose_persist_restart_validate_and_reset(tmp_path):
    store = Store(tmp_path / "filters.sqlite")
    api, session = CatalogAPI(), TelegramSession()
    calls = []
    items = [
        {"id": "match", "name": "<Товар_%>", "price": 150, "priority": "PREMIUM"},
        {"id": "ordinary", "name": "Товар_%", "price": 150, "priority": "DEFAULT"},
        {"id": "expensive", "name": "Товар_%", "price": 500, "priority": "PREMIUM"},
        {"id": "unpriced", "name": "Товар_%", "price": None, "priority": "PREMIUM"},
    ]

    for value in items:
        value["status"] = "APPROVED"

    async def call(method, **kwargs):
        calls.append((method, kwargs))
        for value in items:
            value["status"] = kwargs["statuses"][0].name if kwargs.get("statuses") else "APPROVED"
        return {"items": items, "page_info": {"has_next_page": True, "end_cursor": "next"}}

    api.call = call
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)

    def item_buttons():
        return {
            button.callback_data
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("item:")
        }

    try:
        await ui.message("/start")
        await ui.click("items")
        await ui.click("il:search")
        await ui.message("товар_%")
        await ui.click("il:price")
        before = len(calls)
        await ui.message("200 100")
        assert len(calls) == before
        assert "Минимальная" in session.calls[-1].text
        await ui.message("100 200")
        assert item_buttons() == {"item:match", "item:ordinary"}
        await ui.click("il:PREMIUM")
        assert item_buttons() == {"item:match"}
        assert "Совпало: 1" in session.calls[-1].text
        assert "постраничная" in session.calls[-1].text
        await ui.click("il:category")
        await ui.message("category-id")
        assert calls[-1][1]["category_id"] == "category-id"
        await ui.click("itemfilter:DRAFT")
        assert calls[-1][1]["category_id"] == "category-id"
        assert calls[-1][1]["statuses"][0].name == "DRAFT"
        assert item_buttons() == {"item:match"}
        await dp.storage.close()
        dp = dispatcher(store, api)
        ui = Interaction(bot, dp)
        await ui.click("il:back")
        assert item_buttons() == {"item:match"}
        assert calls[-1][1]["after_cursor"] is None
        await ui.click("il:search")
        await ui.message("не существует")
        assert item_buttons() == set()
        assert "продолжение доступно" in session.calls[-1].text
        assert "ещё не загруженным" in session.calls[-1].text
        before = len(calls)
        await ui.click("il:reset", owner=8)
        assert len(calls) == before
        await ui.click("il:reset")
        assert item_buttons() == {"item:match", "item:ordinary", "item:expensive", "item:unpriced"}
        assert "category_id" not in calls[-1][1]
        assert calls[-1][1]["statuses"][0].name == "APPROVED"
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await dp.storage.close()
        await bot.session.close()
        store.close()


async def test_invalid_filter_input_preserves_active_state(tmp_path):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from app.telegram.listings_panel import ListingsPanel
    from app.telegram.storage import SQLiteStorage

    store = Store(tmp_path / "input.sqlite")
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    panel = ListingsPanel(CatalogAPI())
    try:
        await state.set_data({"item_filters": {"minimum": "10", "priority": "DEFAULT"}})
        await panel.filter_action("il:category", state)
        before = await state.get_data()
        with pytest.raises(ValidationError):
            await panel.filter_message(SimpleNamespace(text="not valid"), state)
        assert await state.get_data() == before
        assert await state.get_state() == "itemlist:category"
    finally:
        store.close()


async def test_category_filter_native_wire_keeps_user_scope(tmp_path):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from app.telegram.storage import SQLiteStorage

    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={"data": {"items": {"edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
        )

    account = SafeAccount("token=private", client=httpx.Client(transport=httpx.MockTransport(respond)))
    account.id = "seller"
    store = Store(tmp_path / "native.sqlite")
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await state.set_data(
            {
                "item_filters": {
                    "category_id": "category-id",
                    "status": "DRAFT",
                    "minimum": "10",
                    "priority": "PREMIUM",
                }
            }
        )
        await ListingsPanel(Playerok(account)).show("items", state)
        assert len(captured) == 1
        variables = json.loads(captured[0].url.params["variables"])
        assert variables["filter"] == {
            "userId": "seller",
            "status": ["DRAFT"],
            "gameCategoryId": "category-id",
        }
        assert "price" not in variables["filter"] and "priority" not in variables["filter"]
        assert not account.writes_enabled
    finally:
        account.close()
        store.close()
