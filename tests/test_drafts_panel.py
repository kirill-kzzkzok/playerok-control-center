from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, Store, encode
from app.services.catalog import draft_page
from app.services.products import ValidationError
from app.telegram.drafts_panel import DraftsPanel
from app.telegram.storage import SQLiteStorage
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "drafts.sqlite")
    s.claim_first_owner(7)
    yield s
    s.close()


def populate(store, count=57):
    states = ["DRAFT", "QUEUED", "REMOTE_DRAFT", "UPDATED", "CANCELLED", "FUTURE_STATUS"]
    with store.transaction() as db:
        for i in range(count):
            db.execute(
                "INSERT INTO drafts(id,owner_id,state,payload,created_at) VALUES(?,?,?,?,?)",
                (
                    f"{i:032d}",
                    7,
                    states[i % len(states)],
                    encode({"name": f"Product {i} literal_%", "fields": {"secret": "PRIVATE_DELIVERY_DATA"}}),
                    (datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=i // 2)).isoformat(),
                ),
            )
        db.execute(
            "INSERT INTO drafts(id,owner_id,state,payload,created_at) VALUES('foreign',8,'DRAFT',?,?)",
            (encode({"name": "Product foreign"}), datetime.now(UTC).isoformat()),
        )


def test_all_pages_reachable_with_stable_tie_breaker_and_owner_isolation(store):
    populate(store)
    ids = []
    for page in range(6):
        result = draft_page(store, 7, page)
        assert result["total"] == 57
        assert result["pages"] == 6
        assert result["has_previous"] == (page > 0)
        assert result["has_next"] == (page < 5)
        ids.extend(row["id"] for row in result["rows"])
        assert all(set(row) == {"id", "name", "state", "created_at"} for row in result["rows"])
    assert len(ids) == len(set(ids)) == 57
    assert "foreign" not in ids
    assert ids == sorted(ids, reverse=True)
    assert draft_page(store, 7, 100)["page"] == 5
    store.execute("DELETE FROM drafts WHERE owner_id=7")
    assert draft_page(store, 7, 5)["page"] == 0
    assert draft_page(store, 7)["total"] == 0


def test_filters_and_literal_sql_search_exclude_private_fields(store):
    populate(store)
    assert draft_page(store, 7, filter_name="DRAFT")["total"] == 10
    assert draft_page(store, 7, filter_name="OTHER")["total"] == 9
    assert draft_page(store, 7, search="_%")["total"] == 57
    assert draft_page(store, 7, search="' OR 1=1 --")["total"] == 0
    assert draft_page(store, 7, search="PRIVATE_DELIVERY_DATA")["total"] == 0
    assert draft_page(store, 7, search="00000000000000000000000000000056")["total"] == 1
    for kwargs in [
        {"page": -1},
        {"page": True},
        {"filter_name": "invalid"},
        {"search": "x" * 201},
        {"page_size": 31},
    ]:
        with pytest.raises(ValidationError):
            draft_page(store, 7, **kwargs)


async def test_dispatcher_search_filter_restart_old_draft_and_no_remote_calls(store):
    populate(store)
    session, api = TelegramSession(), CatalogAPI()
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("drafts")
        assert "Всего: 57" in session.calls[-1].text
        await ui.click("dl:page:5:ALL")
        assert "6/6" in session.calls[-1].text
        ids = [
            b.callback_data
            for row in session.calls[-1].reply_markup.inline_keyboard
            for b in row
            if b.callback_data.startswith("draft:")
        ]
        assert "draft:" + "0" * 32 in ids
        await ui.click("dl:filter:CANCELLED")
        assert "Всего: 9" in session.calls[-1].text
        await ui.click("dl:search")
        assert await state.get_state() == "draftlist:search"
        await dp.storage.close()
        dp = dispatcher(store, api)
        ui = Interaction(bot, dp)
        await ui.message("Product 4 ")
        assert "Поиск: Product 4" in session.calls[-1].text
        assert "Всего: 3" in session.calls[-1].text
        await ui.click("draft:" + f"{4:032d}")
        assert "CANCELLED" in session.calls[-1].text
        await ui.click("dl:search")
        await ui.message("<x>&")
        assert "&lt;x&gt;&amp;" in session.calls[-1].text
        await ui.click("drafts")
        assert "Всего: 57" in session.calls[-1].text
        await ui.click("dl:page:2:ALL", owner=8)
        assert session.calls[-1].__api_method__ == "answerCallbackQuery"
        assert not api.calls
        assert not store.rows("SELECT * FROM outbox")
        assert all(
            "PRIVATE_DELIVERY_DATA" not in (getattr(method, "text", "") or "") for method in session.calls
        )
        assert all(
            len(b.callback_data.encode()) <= 64
            for method in session.calls
            if getattr(method, "reply_markup", None)
            for row in method.reply_markup.inline_keyboard
            for b in row
            if b.callback_data
        )
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_service_panel_rejects_foreign_owner_even_without_middleware(store):
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=8, user_id=8))
    with pytest.raises(Conflict):
        await DraftsPanel(store).callback("drafts", 8, state)
