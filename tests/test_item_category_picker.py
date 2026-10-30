from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store
from app.playerok.adapter import APIError
from app.services.products import ValidationError
from app.telegram.item_category_picker import ItemCategoryPicker
from app.telegram.storage import SQLiteStorage
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "picker.sqlite")
    yield value
    value.close()


def state(store):
    return FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))


def buttons(markup, command="pick"):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data.startswith("icp:") and button.callback_data.split(":")[2] == command
    ]


def games(ids, next_cursor=None):
    return {
        "games": [{"id": ident, "name": "Game " + ident} for ident in ids],
        "page_info": {"has_next_page": next_cursor is not None, "end_cursor": next_cursor},
    }


def categories(count=23):
    return {
        "id": "game",
        "categories": [
            {"id": f"category-{index}", "name": "Same name" if index < 2 else "Category " + str(index)}
            for index in range(count)
        ],
    }


async def test_games_paging_opaque_cursor_and_restart_category_pages_preserve_filters(store):
    api = SimpleNamespace(
        call=AsyncMock(side_effect=[games(["first"], "opaque"), games(["game"]), categories(), categories()])
    )
    picker, context = ItemCategoryPicker(api), state(store)
    await context.set_data(
        {"item_filters": {"priority": "PREMIUM", "minimum": "10", "status": "DRAFT"}, "list_next": "old"}
    )
    await picker.start(context)
    _, markup = await picker.search("Game", context)
    old = buttons(markup)[0]
    _, markup = await picker.action(buttons(markup, "next")[0], context)
    assert api.call.call_args.kwargs["after_cursor"] == "opaque"
    with pytest.raises(ValidationError, match="устарел"):
        await picker.action(old, context)
    _, markup = await picker.action(buttons(markup)[0], context)
    assert len(buttons(markup)) == 10
    restart = state(store)
    _, markup = await ItemCategoryPicker(api).action(buttons(markup, "page")[0], restart)
    assert len(buttons(markup)) == 10
    assert await picker.action(buttons(markup)[3], restart) is None
    value = await restart.get_data()
    assert value == {
        "item_filters": {
            "priority": "PREMIUM",
            "minimum": "10.00",
            "status": "DRAFT",
            "search": "",
            "category_id": "category-13",
        }
    }
    assert await restart.get_state() is None
    with pytest.raises(ValidationError, match="устарел"):
        await picker.action(buttons(markup)[3], restart)
    assert api.call.await_count == 4


async def test_duplicate_category_names_select_by_snapshot_id_and_revalidate_membership(store):
    api = SimpleNamespace(
        call=AsyncMock(side_effect=[games(["game"]), categories(2), {"id": "game", "categories": []}])
    )
    picker, context = ItemCategoryPicker(api), state(store)
    await picker.start(context)
    _, markup = await picker.search("Game", context)
    _, markup = await picker.action(buttons(markup)[0], context)
    before = await context.get_data()
    with pytest.raises(ValidationError, match="больше не доступна"):
        await picker.action(buttons(markup)[1], context)
    assert await context.get_data() == before
    assert not before["item_filters"].get("category_id")


@pytest.mark.parametrize(
    "bad",
    [
        {"games": []},
        games(["game"], "loop"),
        {"games": [{"id": "game", "name": None}], "page_info": {"has_next_page": False}},
    ],
)
async def test_bad_or_looping_game_page_preserves_checkpoint(store, bad):
    api = SimpleNamespace(call=AsyncMock(side_effect=[games(["game"], "loop"), bad]))
    picker, context = ItemCategoryPicker(api), state(store)
    await picker.start(context)
    _, markup = await picker.search("Game", context)
    before = await context.get_data()
    with pytest.raises(APIError):
        await picker.action(buttons(markup, "next")[0], context)
    assert await context.get_data() == before


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "other", "categories": []},
        {"id": "game"},
        {"id": "game", "categories": [{"id": "bad", "name": None}]},
    ],
)
async def test_mismatched_or_incomplete_game_does_not_apply_filter(store, bad):
    api = SimpleNamespace(call=AsyncMock(side_effect=[games(["game"]), bad]))
    picker, context = ItemCategoryPicker(api), state(store)
    await picker.start(context)
    _, markup = await picker.search("Game", context)
    before = await context.get_data()
    with pytest.raises(APIError):
        await picker.action(buttons(markup)[0], context)
    assert await context.get_data() == before


async def test_empty_results_cancel_and_invalid_input_make_no_extra_requests(store):
    api = SimpleNamespace(call=AsyncMock(return_value=games([])))
    picker, context = ItemCategoryPicker(api), state(store)
    await picker.start(context)
    for text in ["", " ", "x" * 101]:
        with pytest.raises(ValidationError):
            await picker.search(text, context)
    api.call.assert_not_awaited()
    text, markup = await picker.search("unknown", context)
    assert "игр нет" in text
    assert buttons(markup) == []
    await context.set_state(None)
    with pytest.raises(ValidationError):
        await picker.action("icp:fake:pick:0", context)
    assert api.call.await_count == 1


async def test_dispatcher_game_category_selection_restart_owner_and_old_callback(store):
    api, session = CatalogAPI(), TelegramSession()
    calls = []

    async def call(method, **kwargs):
        calls.append((method, kwargs))
        if method == "get_games":
            return games(["game"])
        if method == "get_game":
            return categories(2)
        if method == "get_my_items":
            return {"items": [], "page_info": {"has_next_page": False}}
        raise AssertionError(method)

    api.call = call
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        await ui.message("/start")
        await ui.click("il:pickcategory")
        await ui.message("<Game>")
        choice = buttons(session.calls[-1].reply_markup)[0]
        assert "&lt;Game&gt;" in session.calls[-1].text
        before = len(calls)
        await ui.click(choice, owner=8)
        assert len(calls) == before
        await ui.click(choice)
        category = buttons(session.calls[-1].reply_markup)[1]
        await dp.storage.close()
        dp = dispatcher(store, api)
        ui = Interaction(bot, dp)
        await ui.click(category)
        assert calls[-1][0] == "get_my_items"
        assert calls[-1][1]["category_id"] == "category-1"
        before = len(calls)
        await ui.click(category)
        assert "устарел" in session.calls[-1].text
        assert len(calls) == before
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await dp.storage.close()
        await bot.session.close()
