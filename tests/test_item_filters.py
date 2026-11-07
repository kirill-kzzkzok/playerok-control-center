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
        value["category"] = {"id": "category-id"}
    items.append({**items[0], "id": "draft-match", "status": "DRAFT"})

    async def call(method, **kwargs):
        calls.append((method, kwargs))
        selected = {s.name for s in kwargs.get("statuses") or []}
        return {
            "items": [item for item in items if not selected or item["status"] in selected],
            "page_info": {"has_next_page": False},
        }

    api.call = call
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)

    def item_buttons():
        return {
            button.callback_data
            for row in session.last_panel.reply_markup.inline_keyboard
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
        assert item_buttons() == {"item:match", "item:ordinary", "item:draft-match"}
        await ui.click("il:PREMIUM")
        assert item_buttons() == {"item:match", "item:draft-match"}
        assert "Совпало: 2" in session.calls[-1].text
        assert "по снимку каталога" in session.calls[-1].text
        reads = len(calls)
        await ui.click("il:category")
        await ui.message("category-id")
        assert item_buttons() == {"item:match", "item:draft-match"}
        assert len(calls) == reads
        await ui.click("itemfilter:DRAFT")
        # Explicit seller tabs discard hidden search restrictions and read the
        # requested server status; legacy filter composition above stays cached.
        assert len(calls) == reads + 1
        assert [s.name for s in calls[-1][1]["statuses"]] == ["DRAFT"]
        assert item_buttons() == {"item:draft-match"}
        await dp.storage.close()
        dp = dispatcher(store, api)
        ui = Interaction(bot, dp)
        await ui.click("il:back")
        assert item_buttons() == {"item:draft-match"}
        assert calls[-1][1]["after_cursor"] is None
        await ui.click("il:search")
        await ui.message("не существует")
        assert item_buttons() == set()
        assert "Совпало: 0" in session.calls[-1].text
        assert "Поиск не завершён" not in session.calls[-1].text
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


async def test_dispatcher_reopens_known_catalog_page_during_outage(tmp_path):
    from app.playerok.adapter import APIError
    from app.services.item_filters import refresh_due_catalog

    store = Store(tmp_path / "catalog.sqlite")
    api, session = CatalogAPI(), TelegramSession()
    store.set_setting("playerok:account_id", api.account.id)
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(store, api))
    calls = []

    items = [
        {"id": f"lot-{i}", "name": f"Known item {i} [ABC]", "price": 100, "status": "APPROVED"}
        for i in range(101)
    ]

    async def catalog(method, **kwargs):
        calls.append(method)
        start = int(kwargs["after_cursor"] or 0)
        end = min(start + kwargs["count"], len(items))
        return {
            "items": items[start:end],
            "page_info": {
                "has_next_page": end < len(items),
                "end_cursor": str(end) if end < len(items) else None,
            },
        }

    try:
        api.call = catalog
        await ui.message("/start")
        await ui.click("items")
        assert "Known item" in str(session.last_panel.reply_markup)

        async def offline(method, **kwargs):
            calls.append(method)
            raise APIError("NETWORK_ERROR")

        assert len(calls) == 1
        api.call = offline
        await ui.click("home")
        await ui.click("items")
        assert "Known item" in str(session.last_panel.reply_markup)
        assert "Снимок списка" in session.last_panel.text
        assert len(calls) == 1
        api.call = catalog
        for _ in range(5):
            await refresh_due_catalog(store, api)
        assert len(calls) == 6
        assert len(store.setting("ui:catalog:" + api.account.id)["items"]) == 101
        calls.clear()
        api.call = offline
        await ui.click("home")
        await ui.click("items")
        assert "Known item" in str(session.last_panel.reply_markup)
        assert "по снимку каталога" in session.last_panel.text
        next_page = next(
            b.callback_data
            for row in session.last_panel.reply_markup.inline_keyboard
            for b in row
            if b.callback_data.startswith("listnext:")
        )
        await ui.click(next_page)
        assert "item:lot-10" in str(session.last_panel.reply_markup)
        assert "item:lot-0'" not in str(session.last_panel.reply_markup)
        await ui.click("il:search")
        await ui.message("ABC")
        assert "Совпало: 101" in session.last_panel.text
        assert not calls
    finally:
        await bot.session.close()
        store.close()
