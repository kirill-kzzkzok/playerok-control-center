from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store
from app.services.products import ValidationError
from app.telegram.item_panel import ItemPanel, counter
from app.telegram.storage import SQLiteStorage
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher


def item(**changes):
    return {
        "id": "lot",
        "user": {"id": "seller"},
        "name": "<Item>",
        "price": 100,
        "status": "APPROVED",
        "priority": "PREMIUM",
        "priority_position": 1,
        "deals_counter": 0,
        "views_counter": 12,
        "description": "<Description>",
        "attachments": [
            {"id": "one", "url": "https://playerok.com/one.jpg"},
            {"id": "two", "url": "https://playerok.com/two.jpg"},
        ],
        **changes,
    }


@pytest.fixture
def context(tmp_path):
    store = Store(tmp_path / "item.sqlite")
    yield store, FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    store.close()


async def test_card_metadata_gallery_restart_and_stable_image_ids(context):
    store, state = context
    value = item()
    api = SimpleNamespace(
        account=SimpleNamespace(id="seller"), call=AsyncMock(side_effect=lambda *a, **k: deepcopy(value))
    )
    panel = ItemPanel(api)
    _, text, rows = await panel.card("lot", state)
    assert "&lt;Item&gt;" in text and "&lt;Description&gt;" in text
    assert "Сделки по API: 0" in text
    assert "Позиция внутри приоритета: 1" in text
    assert "Просмотры по API: 12" in text
    callback = rows[0][0][1]
    value["attachments"].reverse()
    restarted = FSMContext(SQLiteStorage(store), state.key)
    panel.attachments.remote = AsyncMock(return_value=b"jpeg")
    content, caption, markup = await panel.media(callback, restarted)
    assert content == b"jpeg" and "Фото 1 из 2" in caption
    panel.attachments.remote.assert_awaited_once_with("https://playerok.com/one.jpg")
    next_photo = markup.inline_keyboard[0][0].callback_data
    await panel.media(next_photo, restarted)
    assert panel.attachments.remote.call_args.args[0] == "https://playerok.com/two.jpg"


@pytest.mark.parametrize(
    "change",
    [{"user": {"id": "other"}}, {"id": "other"}, {"is_attachments_forbidden": True}, {"attachments": []}],
)
async def test_gallery_rechecks_owner_identity_hidden_and_removed_before_download(context, change):
    _, state = context
    api = SimpleNamespace(account=SimpleNamespace(id="seller"), call=AsyncMock(return_value=item()))
    panel = ItemPanel(api)
    _, _, rows = await panel.card("lot", state)
    api.call.return_value = item(**change)
    panel.attachments.remote = AsyncMock()
    with pytest.raises(ValidationError):
        await panel.media(rows[0][0][1], state)
    panel.attachments.remote.assert_not_awaited()


async def test_stale_gallery_and_bad_index_never_call_api(context):
    _, state = context
    api = SimpleNamespace(account=SimpleNamespace(id="seller"), call=AsyncMock(return_value=item()))
    panel = ItemPanel(api)
    _, _, old_rows = await panel.card("lot", state)
    _, _, rows = await panel.card("lot", state)
    before = api.call.await_count
    nonce = rows[0][0][1].split(":")[1]
    for action in [
        old_rows[0][0][1],
        f"itemmedia:{nonce}:-1",
        f"itemmedia:{nonce}:2",
        f"itemmedia:{nonce}:bad",
    ]:
        with pytest.raises(ValidationError):
            await panel.media(action, state)
    assert api.call.await_count == before


async def test_hidden_no_image_missing_counters_and_unknown_priority_are_honest(context):
    _, state = context
    api = SimpleNamespace(
        account=SimpleNamespace(id="seller"),
        call=AsyncMock(
            return_value=item(
                is_attachments_forbidden=True, deals_counter=None, views_counter=-1, priority=None
            )
        ),
    )
    _, text, rows = await ItemPanel(api).card("lot", state)
    assert rows == [] and "Фото скрыты Playerok" in text
    assert "Сделки по API: нет данных" in text and "Приоритет: нет данных" in text
    assert counter(False) == counter(-1) == counter("1") == "нет данных"


async def test_dispatcher_card_gallery_and_foreign_owner_without_mutations(context, monkeypatch):
    store, _ = context
    api, session = CatalogAPI(), TelegramSession()
    api.call = AsyncMock(return_value=item())
    download = AsyncMock(return_value=b"jpeg")
    monkeypatch.setattr("app.telegram.item_panel.Attachments.remote", download)
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        await ui.message("/start")
        await ui.click("item:lot")
        sent = session.calls[-1]
        assert "Опубликован" in sent.text
        await ui.click("iv:details:lot")
        sent = session.calls[-1]
        assert "Сделки по API: 0" in sent.text
        callback = next(
            button.callback_data
            for row in sent.reply_markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("itemmedia:")
        )
        count = api.call.await_count
        await ui.click(callback, owner=8)
        assert api.call.await_count == count
        await ui.click(callback)
        assert session.calls[-1].__api_method__ == "sendPhoto"
        assert "&lt;Item&gt;" in session.calls[-1].caption
        download.assert_awaited_once()
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await dp.storage.close()
        await bot.session.close()
