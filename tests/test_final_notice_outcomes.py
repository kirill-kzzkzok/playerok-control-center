"""Telegram rejection differs from an unconfirmed send; only rejection can retry."""

import sqlite3
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from app.database.store import Store
from app.telegram.operation_results import OperationResults
from app.workers.notifications import Notifications
from tests.test_telegram_integration import TelegramSession


@pytest.mark.parametrize("receipt", [False, True])
@pytest.mark.parametrize(
    "failure,outcome",
    [("rate", "PENDING"), ("forbidden", "REJECTED"), ("bad", "REJECTED"), ("timeout", "UNCERTAIN")],
)
async def test_confirmed_rejection_and_unknown_outcome_have_distinct_recovery(
    tmp_path, receipt, failure, outcome
):
    store = Store(tmp_path / "notice.sqlite")
    store.claim_first_owner(7)
    store.set_setting("notifications:initialized", True)
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    operation = store.enqueue("unknown", "reply", "chat", {"text": "private"}, actor=7 if receipt else None)
    action = store.claim()
    store.finish(operation, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
    errors = {
        "rate": TelegramRetryAfter(SendMessage(chat_id=7, text="x"), "private", retry_after=30),
        "forbidden": TelegramForbiddenError(SendMessage(chat_id=7, text="x"), "private"),
        "bad": TelegramBadRequest(SendMessage(chat_id=7, text="x"), "private"),
        "timeout": TimeoutError(),
    }
    original = session.make_request
    attempts = []

    async def send(bot, method, timeout=None):
        if isinstance(method, SendMessage):
            attempts.append(method)
            if len(attempts) == 1:
                raise errors[failure]
        return await original(bot, method, timeout)

    session.make_request = send
    run = OperationResults(store).notify if receipt else Notifications(store, bot).once
    try:
        await run(bot) if receipt else await run()
        table, status = ("operation_receipts", "notice_status") if receipt else ("notifications", "status")
        row = store.rows(f"SELECT * FROM {table}")[0]
        assert row[status] == outcome
        assert "private" not in str(row.get("send_error"))
        await run(bot) if receipt else await run()
        assert len(attempts) == 1
        if failure == "rate":
            store.execute(f"UPDATE {table} SET send_retry_at=0")
            await run(bot) if receipt else await run()
            assert len(attempts) == 2
            assert store.rows(f"SELECT {status} status FROM {table}")[0]["status"] == (
                "SENT" if receipt else "SUCCEEDED"
            )
        assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
    finally:
        await bot.session.close()
        store.close()


def test_old_receipt_schema_migrates_without_losing_unknown_receipts_or_views(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    directory = Path(__file__).resolve().parents[1] / "app" / "database"
    db.executescript((directory / "schema.sql").read_text(encoding="utf-8"))
    for source in sorted((directory / "migrations").glob("*.sql")):
        version = int(source.name.split("_", 1)[0])
        if version >= 33:
            break
        db.executescript(source.read_text(encoding="utf-8"))
        db.execute("INSERT INTO schema_migrations VALUES(?,?)", (version, "2026-11-01T00:00:00+00:00"))
        db.commit()
    db.close()
    # Insert historical receipts using the actual v32 columns.
    db = sqlite3.connect(path)
    db.execute("INSERT INTO administrators VALUES(7)")
    db.execute(
        "INSERT INTO outbox(id,operation_key,kind,target,payload,status,created_at) VALUES('op','op','reply','chat','{}','UNCERTAIN','2026-11-01')"
    )
    db.execute("INSERT INTO operation_receipts VALUES('op',7,'UNCERTAIN',123,124,'token','UNCERTAIN')")
    db.commit()
    db.close()
    store = Store(path)
    try:
        row = store.rows("SELECT * FROM operation_receipts")[0]
        assert row["notice_status"] == "UNCERTAIN" and row["telegram_message_id"] == 123
        assert row["view_message_id"] == 124 and row["view_token"] == "token"
        assert row["send_retry_at"] == 0 and row["send_error"] is None
        assert not store.rows("PRAGMA foreign_key_check")
    finally:
        store.close()
