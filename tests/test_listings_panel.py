import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store
from app.playerok.adapter import APIError
from app.services.products import ValidationError
from app.telegram.listings_panel import ListingsPanel
from app.telegram.storage import SQLiteStorage
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "lists.sqlite")
    yield value
    value.close()


def page(kind, ids=(), cursor=None):
    return {
        kind: [{"id": ident, "name": ident, "status": "APPROVED"} for ident in ids],
        "page_info": {"has_next_page": cursor is not None, "end_cursor": cursor},
    }


def next_button(markup):
    return next(
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data.startswith("listnext:")
    )


@pytest.mark.parametrize("kind", ["items", "chats"])
async def test_pages_survive_restart_and_last_page_clears_cursor_and_rejects_old_button(store, kind):
    api = SimpleNamespace(call=AsyncMock(side_effect=[page(kind, ["first"], "opaque"), page(kind, ["last"])]))
    key = StorageKey(bot_id=123, chat_id=7, user_id=7)
    state = FSMContext(SQLiteStorage(store), key)
    panel = ListingsPanel(api)
    _, markup = await panel.show(kind, state)
    nonce = next_button(markup).split(":")[2]
    restarted = FSMContext(SQLiteStorage(store), key)
    text, markup = await panel.show(kind, restarted, nonce=nonce)
    assert "страница 2" in text
    data = await restarted.get_data()
    assert data["list_next"] is data["list_nonce"] is None
    assert data["list_seen"] == []
    with pytest.raises(ValidationError, match="устарел"):
        await panel.show(kind, restarted, nonce=nonce)
    assert api.call.await_count == 2
    assert api.call.call_args.kwargs["after_cursor"] == "opaque"
    assert not any(
        button.callback_data.startswith("listnext:") for row in markup.inline_keyboard for button in row
    )


@pytest.mark.parametrize("kind", ["items", "chats"])
async def test_repeated_cursor_and_invalid_envelope_preserve_previous_checkpoint(store, kind):
    api = SimpleNamespace(call=AsyncMock(return_value=page(kind, ["first"], "cursor")))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    panel = ListingsPanel(api)
    await panel.show(kind, state)
    before = await state.get_data()
    for response in [
        page(kind, ["second"], "cursor"),
        {kind: []},
        {kind: [{"name": "no-id"}], "page_info": {"has_next_page": False}},
        {kind: [], "page_info": {"has_next_page": "false"}},
    ]:
        api.call.return_value = response
        with pytest.raises(APIError):
            await panel.show(kind, state, nonce=before["list_nonce"])
        assert await state.get_data() == before
    api.call.return_value = page(kind)
    await panel.show(kind, state, nonce=before["list_nonce"])
    assert (await state.get_data())["list_next"] is None


async def test_empty_continuation_does_not_claim_end_and_old_nonce_cannot_advance(store):
    api = SimpleNamespace(
        call=AsyncMock(side_effect=[page("items", cursor="a"), page("items", ["second"], "b")])
    )
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    panel = ListingsPanel(api)
    text, markup = await panel.show("items", state)
    assert "продолжение доступно" in text
    old = next_button(markup).split(":")[2]
    await panel.show("items", state, nonce=old)
    with pytest.raises(ValidationError, match="устарел"):
        await panel.show("items", state, nonce=old)
    with pytest.raises(ValidationError, match="устарел"):
        await panel.show("chats", state, nonce=(await state.get_data())["list_nonce"])
    assert api.call.await_count == 2


async def test_long_id_or_loop_limit_does_not_replace_checkpoint(store):
    api = SimpleNamespace(call=AsyncMock(return_value=page("items", ["x" * 65], "a")))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    panel = ListingsPanel(api)
    with pytest.raises(ValidationError, match="Идентификатор"):
        await panel.show("items", state)
    assert await state.get_data() == {}
    await state.set_data({"list_kind": "items", "list_nonce": "test", "list_next": "a", "list_number": 1000})
    with pytest.raises(ValidationError, match="лимит"):
        await panel.show("items", state, nonce="test")
    assert api.call.await_count == 1


async def test_dispatcher_double_click_filter_reset_legacy_button_and_foreign_owner(store):
    api, session = CatalogAPI(), TelegramSession()
    requests = []

    async def call(method, **kwargs):
        requests.append((method, kwargs))
        if kwargs.get("statuses") and kwargs["statuses"][0].name != "APPROVED":
            result = page("items", ["filtered"])
            result["items"][0]["status"] = kwargs["statuses"][0].name
            return result
        if kwargs.get("after_cursor"):
            return page("items", ["second"], "b")
        return page("items", ["first"], "a")

    api.call = call
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        await ui.message("/start")
        await ui.click("items")
        old = next_button(session.calls[-1].reply_markup)
        await asyncio.gather(ui.click(old), ui.click(old))
        assert len(requests) == 2
        assert "устарел" in session.calls[-1].text
        state = FSMContext(dp.storage, StorageKey(bot_id=123, chat_id=7, user_id=7))
        current = (await state.get_data())["list_nonce"]
        await ui.click("itemfilter:DRAFT")
        assert requests[-1][1]["after_cursor"] is None
        assert requests[-1][1]["statuses"][0].name == "DRAFT"
        count = len(requests)
        await ui.click("listnext:items:" + current)
        await ui.click("listnext:items")
        await ui.click(old, owner=8)
        assert len(requests) == count
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await dp.storage.close()
        await bot.session.close()
