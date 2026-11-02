"""Audit regressions use real Dispatcher and stateful Bot API boundary."""

from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store
from app.telegram.storage import SQLiteStorage
from tests.stateful_telegram import StatefulTelegram
from tests.test_telegram_integration import Interaction
from tests.test_telegram_transport import production_dispatcher
from tests.test_ux_redesign import ShopAPI


@pytest.fixture
async def ui(tmp_path):
    store, api, session = Store(tmp_path / "ui.sqlite"), ShopAPI(), StatefulTelegram()
    store.claim_first_owner(7)
    bot = Bot("123:" + "x" * 35, session=session)
    interaction = Interaction(bot, production_dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    await interaction.message("/start")
    yield SimpleNamespace(store=store, api=api, session=session, bot=bot, ui=interaction, state=state)
    await session.close()
    store.close()


async def test_ack_rejection_preserves_confirmation_and_panel(ui):
    r = ui
    await r.state.set_data(
        {
            "confirm_token": "confirmed",
            "pending": {"kind": "reply", "target": "chat", "payload": {"text": "hello"}},
        }
    )
    before = r.store.setting("navigation:123:7")
    data = await r.state.get_data()
    r.session.inject("answerCallbackQuery")
    await r.ui.click("commit:confirmed")
    assert r.store.setting("navigation:123:7") == before
    assert await r.state.get_data() == data
    assert not r.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("unavailable", ["deleted", "uneditable"])
async def test_start_replaces_definitely_unavailable_panel(ui, unavailable):
    r = ui
    old = r.store.setting("navigation:123:7")["id"]
    r.session.messages[old]["exists" if unavailable == "deleted" else "editable"] = False
    await r.ui.message("/start")
    panel = r.store.setting("navigation:123:7")
    assert panel["id"] != old
    assert r.session.messages[panel["id"]]["exists"]
    assert not panel["sending"]


async def test_notification_buttons_authorized_by_exact_origin(ui):
    from app.services.orders import Orders
    from app.workers.notifications import Notifications

    r = ui
    r.store.set_setting("notifications:initialized", True)
    Orders(r.store).ingest_message(
        "chat", {"id": "incoming", "text": "hello", "user": {"id": "buyer", "username": "Buyer"}}
    )
    await Notifications(r.store, r.bot).once()
    ident = r.store.rows("SELECT telegram_message_id FROM notifications")[0]["telegram_message_id"]
    panel = r.store.setting("navigation:123:7")["id"]
    assert ident != panel
    await r.ui.click("readchat:chat", message_id=ident)
    assert not (await r.state.get_data()).get("pending")
    assert len(r.store.rows("SELECT * FROM chat_cards")) == 1
    assert not r.store.rows("SELECT * FROM outbox")


async def test_wizard_local_back_survives_lost_ack_without_submitting(ui):
    from tests.test_telegram_integration import CatalogAPI

    r = ui
    original = r.api.call
    catalog = CatalogAPI()

    async def with_catalog(method, **kwargs):
        if method in {"get_games", "get_game", "get_game_category"}:
            return await catalog.call(method, **kwargs)
        return await original(method, **kwargs)

    r.api.call = with_catalog
    await r.ui.click("create")
    token = (await r.state.get_data())["view_token"]
    await r.ui.click(f"w:{token}:choose:0")
    token = (await r.state.get_data())["view_token"]
    r.session.inject("answerCallbackQuery")
    await r.ui.click(f"w:{token}:back")
    draft = r.store.get_draft((await r.state.get_data())["draft_id"], 7)
    assert draft["payload"]["step"] == "game"
    assert not r.store.rows("SELECT * FROM outbox")
