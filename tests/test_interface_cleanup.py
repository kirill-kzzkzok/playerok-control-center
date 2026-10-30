import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import DeleteMessage

from app.database.store import Conflict, Store
from app.services.interface_cleanup import InterfaceCleanup
from app.telegram.panel_tracking import PanelTracking, panel_owner
from app.telegram.storage import SQLiteStorage
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "interface.sqlite")
    s.claim_first_owner(7)
    yield s
    s.close()


def record(store, ident, *, owner=7, bot_id=123, age=600, touched=600):
    at = time.time()
    store.execute(
        "INSERT INTO panel_messages(bot_id,owner_id,message_id,created_at,touched_at) VALUES(?,?,?,?,?)",
        (bot_id, owner, ident, at - age, at - touched),
    )


def test_preview_only_tracked_old_enough_recent_bot_panels_excludes_notice_current_other_bot(store):
    for i in range(25):
        record(store, i + 10)
    record(store, 1)  # current callback message
    record(store, 2, touched=0)
    record(store, 3, age=48 * 3600)
    record(store, 4, bot_id=456)
    record(store, 5)
    store.execute(
        "INSERT INTO notifications(event_key,owner_id,telegram_message_id,status) VALUES('notice',7,5,'SUCCEEDED')"
    )
    rows = InterfaceCleanup(store).preview(7, 123, 1)
    assert len(rows) == 20
    assert not {1, 2, 3, 4, 5} & {row["message_id"] for row in rows}
    with pytest.raises(Conflict):
        InterfaceCleanup(store).preview(8, 123, 1)


async def test_delete_only_confirmed_snapshot_and_preserve_unknown_historical_messages(store):
    record(store, 10)
    service = InterfaceCleanup(store)
    bot = SimpleNamespace(id=123, delete_message=AsyncMock(return_value=True))
    plan = service.preview(7, 123, 1)
    result = await service.execute(7, bot, 1, plan, "nonce")
    assert result == {"DELETED": 1}
    bot.delete_message.assert_awaited_once_with(chat_id=7, message_id=10)
    with pytest.raises(Conflict):
        await service.execute(7, bot, 1, plan, "nonce")
    assert bot.delete_message.await_count == 1
    assert not service.preview(7, 123, 1)


@pytest.mark.parametrize("change", ["edit", "notification", "unknown", "other_bot"])
async def test_changed_or_forged_snapshot_cannot_delete(store, change):
    record(store, 10)
    service = InterfaceCleanup(store)
    plan = service.preview(7, 123, 1)
    bot = SimpleNamespace(id=123, delete_message=AsyncMock(return_value=True))
    if change == "edit":
        store.execute("UPDATE panel_messages SET touched_at=?", (time.time(),))
    elif change == "notification":
        store.execute(
            "INSERT INTO notifications(event_key,owner_id,telegram_message_id,status) VALUES('new',7,10,'SUCCEEDED')"
        )
    elif change == "unknown":
        plan = [{"message_id": 999}]
    else:
        bot.id = 456
    with pytest.raises(Conflict):
        await service.execute(7, bot, 1, plan, "nonce")
    bot.delete_message.assert_not_called()


@pytest.mark.parametrize("mode", ["timeout", "rate", "rejected", "cancel"])
async def test_deletion_error_or_cancellation_has_durable_outcome_without_resend(store, mode):
    record(store, 10)
    record(store, 11)
    method = DeleteMessage(chat_id=7, message_id=10)
    error = {
        "timeout": TimeoutError(),
        "rate": TelegramRetryAfter(method=method, message="retry", retry_after=100),
        "rejected": TelegramBadRequest(method=method, message="can't delete"),
        "cancel": asyncio.CancelledError(),
    }[mode]
    bot = SimpleNamespace(id=123, delete_message=AsyncMock(side_effect=error))
    service = InterfaceCleanup(store)
    plan = service.preview(7, 123, 1)
    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await service.execute(7, bot, 1, plan, "nonce")
        assert service.recover() == 1
    else:
        await service.execute(7, bot, 1, plan, "nonce")
    rows = store.rows("SELECT status FROM panel_messages")
    assert all(row["status"] in {"UNCERTAIN", "FAILED"} for row in rows)
    assert not service.preview(7, 123, 1)
    assert bot.delete_message.await_count == (2 if mode == "rejected" else 1)


class CleanupSession(TelegramSession):
    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__ == "deleteMessage":
            self.calls.append(method)
            return True
        return await super().make_request(bot, method, timeout)


async def test_tracking_is_scoped_to_authorized_handler_and_contains_no_message_text(store):
    session = CleanupSession()
    session.middleware(PanelTracking(store))
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, CatalogAPI())
    try:
        await bot.send_message(7, "private notification outside UI")
        assert not store.rows("SELECT * FROM panel_messages")
        await Interaction(bot, dp).message("/start")
        rows = store.rows("SELECT * FROM panel_messages")
        assert len(rows) == 1 and rows[0]["owner_id"] == 7
        assert set(rows[0]) == {
            "bot_id",
            "owner_id",
            "message_id",
            "created_at",
            "touched_at",
            "status",
            "cleanup_id",
            "error_code",
        }
        assert panel_owner.get() is None
        await Interaction(bot, dp).click("settings", owner=8)
        assert len(store.rows("SELECT * FROM panel_messages")) == 1
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_cleanup_dispatcher_confirmation_restart_cancel_and_double_commit(store):
    record(store, 50)
    session, api = CleanupSession(), CatalogAPI()
    session.middleware(PanelTracking(store))
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("ic:preview")
        assert "К удалению: 1" in session.calls[-1].text
        assert not any(m.__api_method__ == "deleteMessage" for m in session.calls)
        nonce = (await state.get_data())["interface_cleanup"]["nonce"]
        await ui.click("settings")
        await ui.click("ic:commit:" + nonce)
        assert "устарело" in session.calls[-1].text
        await ui.click("ic:preview")
        nonce = (await state.get_data())["interface_cleanup"]["nonce"]
        await dp.storage.close()
        dp = dispatcher(store, api)
        ui = Interaction(bot, dp)
        await ui.click("ic:commit:" + nonce)
        assert "Удалено: 1" in session.calls[-1].text
        await ui.click("ic:commit:" + nonce)
        assert "устарело" in session.calls[-1].text
        assert len([m for m in session.calls if m.__api_method__ == "deleteMessage"]) == 1
        assert not api.calls
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await dp.storage.close()
        await bot.session.close()
