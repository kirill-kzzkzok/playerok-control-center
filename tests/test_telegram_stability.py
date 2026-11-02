"""Offline failure reproduction; real UI/services, boundary-only transports."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import EditMessageText

from app.database.store import Store
from app.services.orders import Orders
from app.workers.notifications import Notifications
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher


@pytest.mark.parametrize(
    "reason",
    ["message to edit not found", "message can't be edited", "can't parse entities", "message is too long"],
)
async def test_rejected_notification_edit_is_terminal_without_losing_history(tmp_path, reason):
    store = Store(tmp_path / "ui.sqlite")
    store.claim_first_owner(7)
    store.set_setting("sync:health", {"status": "OK"})
    store.set_setting("notifications:initialized", True)
    orders = Orders(store)
    orders.ingest_message("chat", {"id": "m", "text": "before"})
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        edit_message_text=AsyncMock(
            side_effect=TelegramBadRequest(
                method=EditMessageText(text="x", chat_id=7, message_id=1), message=reason
            )
        ),
    )
    worker = Notifications(store, bot)
    await worker.once()
    orders.ingest_message("chat", {"id": "m", "text": "changed"})
    from app.telegram.chat_cards import ChatCards

    await ChatCards(store, bot).flush()
    store.execute("UPDATE chat_cards SET retry_after=0")
    from app.telegram.chat_cards import ChatCards

    await ChatCards(store, bot).flush()
    assert bot.edit_message_text.await_count == 1
    assert store.rows("SELECT status FROM notifications")[0]["status"] == "SUCCEEDED"
    assert not store.rows("SELECT * FROM outbox")
    store.close()


@pytest.mark.parametrize("limited", [False, True])
async def test_definite_bad_content_does_not_leave_editing_pending(tmp_path, limited):
    class Reject(TelegramSession):
        async def make_request(self, bot, method, timeout=None):
            if method.__api_method__ == "editMessageText":
                if limited:
                    raise TelegramRetryAfter(method=method, message="slow down", retry_after=30)
                raise TelegramBadRequest(method=method, message="can't parse entities: private text")
            return await super().make_request(bot, method, timeout)

    store = Store(tmp_path / "nav.sqlite")
    store.claim_first_owner(7)
    bot = Bot("123:" + "x" * 35, session=Reject())
    ui = Interaction(bot, dispatcher(store, CatalogAPI()))
    await ui.click("home")
    try:
        await ui.click("diagnostics")
    except (TelegramBadRequest, TelegramRetryAfter):
        pass
    assert not store.setting("navigation:123:7").get("editing")
    assert not store.rows("SELECT * FROM outbox")
    await bot.session.close()
    store.close()


@pytest.mark.parametrize(
    "message,expected",
    [
        ("message is not modified", "MESSAGE_NOT_MODIFIED"),
        ("message to edit not found", "MESSAGE_NOT_FOUND"),
        ("message can't be edited", "MESSAGE_NOT_EDITABLE"),
        ("message text is empty", "INVALID_MESSAGE_CONTENT"),
        ("message is too long", "MESSAGE_TOO_LONG"),
        ("can't parse entities private-buyer", "ENTITY_PARSE_ERROR"),
        ("message id is invalid", "INVALID_MESSAGE_ID"),
        ("query is too old", "CALLBACK_EXPIRED"),
        ("unexpected-private-content", "UNKNOWN_TELEGRAM_ERROR"),
    ],
)
def test_safe_classification(message, expected):
    from app.telegram.errors import telegram_error

    assert (
        telegram_error(
            TelegramBadRequest(
                method=EditMessageText(text="secret", chat_id=7, message_id=1), message=message
            )
        )
        == expected
    )


def test_transport_groups_failures_and_reports_recovery_without_raw_content(caplog):
    import logging

    from app.telegram.transport import TelegramNetworkSession

    session = TelegramNetworkSession()
    with caplog.at_level(logging.INFO):
        for _ in range(4):
            state, token, started = session._begin("getUpdates")
            state["error_code"] = "NETWORK_ERROR"
            session._finish(state, token, started, "TelegramNetworkError")
        state, token, started = session._begin("getUpdates")
        session._finish(state, token, started, "OK")
        for _ in range(2):
            state, token, started = session._begin("sendMessage")
            session._finish(state, token, started, "OK")
    assert len(caplog.records) == 2
    assert "4 неудачных" in caplog.text
    assert "http=" not in caplog.text


async def test_uncertain_transport_result_never_becomes_automatic_retry(tmp_path):
    from app.playerok.adapter import APIError
    from app.workers.outbox import OutboxWorker

    store = Store(tmp_path / "uncertain.sqlite")
    ident = store.enqueue("uncertain", "reply", "chat", {})
    calls = []

    async def execute(action):
        calls.append(action["id"])
        raise APIError("NETWORK_ERROR", uncertain=True)

    worker = OutboxWorker(store, execute)
    await worker.once()
    assert store.rows("SELECT status FROM outbox WHERE id=?", (ident,))[0]["status"] == "UNCERTAIN"
    await worker.once()
    assert calls == [ident]
    store.close()


async def test_wizard_entry_preserves_previous_fsm_on_data_write_failure(tmp_path):
    import sqlite3

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from app.services.products import Products
    from app.telegram.storage import SQLiteStorage
    from app.telegram.wizard import Wizard

    store = Store(tmp_path / "fsm.sqlite")
    store.claim_first_owner(7)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    await state.set_state("previous")
    await state.set_data({"context": "previous"})
    store.execute(
        "CREATE TEMP TRIGGER reject_fsm_data BEFORE UPDATE OF data ON fsm BEGIN SELECT RAISE(ABORT,'fault'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await Wizard(store, CatalogAPI(), Products(store, CatalogAPI())).start(7, state)
    assert await state.get_state() == "previous"
    assert await state.get_data() == {"context": "previous"}
    store.close()


def test_warning_window_keeps_separate_causes_and_total_count(caplog, monkeypatch):
    import logging

    from app.telegram.transport import TelegramNetworkSession

    clock = [0.0]
    monkeypatch.setattr("app.telegram.transport.time.monotonic", lambda: clock[0])
    session = TelegramNetworkSession()

    def emit(code):
        state, token, started = session._begin("getUpdates")
        if code:
            state["error_code"] = code
        session._finish(state, token, started, "TelegramNetworkError" if code else "OK")

    with caplog.at_level(logging.INFO):
        emit("NETWORK_ERROR")
        emit("NETWORK_ERROR")
        emit("TIMEOUT")
        clock[0] = 31
        emit("NETWORK_ERROR")
        emit(None)
    assert sum("Проблема Telegram сохраняется" in r.message for r in caplog.records) == 1
    assert "3 повторов" in caplog.text
    assert "3 неудачных" in caplog.text and "1 неудачных" in caplog.text
    assert len(caplog.records) == 5


async def test_unknown_notification_rejection_has_finite_refresh_budget(tmp_path):
    store = Store(tmp_path / "budget.sqlite")
    store.claim_first_owner(7)
    store.set_setting("sync:health", {"status": "OK"})
    store.set_setting("notifications:initialized", True)
    orders = Orders(store)
    orders.ingest_message("chat", {"id": "m", "text": "before"})
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        edit_message_text=AsyncMock(
            side_effect=TelegramBadRequest(
                method=EditMessageText(text="x", chat_id=7, message_id=1),
                message="unexpected private response",
            )
        ),
    )
    worker = Notifications(store, bot)
    await worker.once()
    orders.ingest_message("chat", {"id": "m", "text": "after"})
    for _ in range(10):
        store.execute("UPDATE chat_cards SET retry_after=0")
        from app.telegram.chat_cards import ChatCards

        await ChatCards(store, bot).flush()
    assert bot.edit_message_text.await_count == 8
    assert store.rows("SELECT status FROM notifications") == [{"status": "SUCCEEDED"}]
    store.close()
