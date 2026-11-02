from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store
from app.services.products import ValidationError
from app.telegram.chat_panel import ChatPanel
from app.telegram.storage import SQLiteStorage


def page(ident, cursor=None):
    return {
        "messages": [{"id": ident, "text": "<hello>" * 1000, "user": {"username": "<buyer>"}}],
        "page_info": {"has_next_page": cursor is not None, "end_cursor": cursor},
    }


def page_button(markup, direction):
    return next(
        button
        for row in markup.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith("chatpage:")
        and button.callback_data.endswith(":" + direction)
    )


async def test_chat_pagination_survives_storage_restart_and_rejects_old_callback(tmp_path):
    store = Store(tmp_path / "chat.sqlite")
    try:
        state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=2, user_id=2))
        api = SimpleNamespace(call=AsyncMock(side_effect=[page("new", "opaque"), page("old")]))
        panel = ChatPanel(store, api)
        text, markup = await panel.show("chat:chat-id", state)
        assert "&lt;buyer&gt;" in text and len(text) < 4096
        assert [b.text for row in markup.inline_keyboard for b in row] == ["✉️ Ответить", "✖️ Закрыть"]
        older = "chatpage:" + (await state.get_data())["chat_view"]["nonce"] + ":older"
        state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=2, user_id=2))
        text, _ = await ChatPanel(store, api).show(older, state)
        assert "страница 2" in text
        assert api.call.call_args.kwargs["after_cursor"] == "opaque"
        assert len(store.rows("SELECT * FROM chat_messages")) == 2
        assert not store.rows("SELECT * FROM inbox")
        with pytest.raises(ValidationError, match="устарела"):
            await panel.show(older, state)
    finally:
        store.close()


async def test_deleted_message_hides_body_and_incomplete_page_is_not_cached(tmp_path):
    store = Store(tmp_path / "chat.sqlite")
    try:
        state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=2, user_id=2))
        result = page("m")
        result["messages"][0].update(deleted_at="today", text="private deleted body", images=[{}])
        api = SimpleNamespace(call=AsyncMock(return_value=result))
        text, _ = await ChatPanel(store, api).show("chat:chat-id", state)
        assert "private deleted body" not in text
        assert "сообщение удалено" in text
        assert "Изображений: 1" in text
    finally:
        store.close()


async def test_navigation_back_to_newer_page_accepts_known_forward_cursor(tmp_path):
    store = Store(tmp_path / "chat.sqlite")
    try:
        state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=2, user_id=2))
        api = SimpleNamespace(
            call=AsyncMock(side_effect=[page("new", "opaque"), page("old"), page("new", "opaque")])
        )
        panel = ChatPanel(store, api)
        _, markup = await panel.show("chat:chat-id", state)
        _, markup = await panel.show(
            "chatpage:" + (await state.get_data())["chat_view"]["nonce"] + ":older", state
        )
        text, markup = await panel.show(
            "chatpage:" + (await state.get_data())["chat_view"]["nonce"] + ":newer", state
        )
        assert "страница 1" in text
        assert [b.text for row in markup.inline_keyboard for b in row] == ["✉️ Ответить", "✖️ Закрыть"]
        assert api.call.call_args.kwargs["after_cursor"] is None
    finally:
        store.close()
