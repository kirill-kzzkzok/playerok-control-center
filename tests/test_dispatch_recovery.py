import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, Store
from app.playerok.adapter import SafeAccount
from app.services.products import Products
from app.telegram.storage import SQLiteStorage
from app.workers.outbox import Executor, OutboxWorker
from tests.test_operations import FakeAPI
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "late.sqlite")
    value.claim_first_owner(7)
    yield value
    value.close()


@pytest.mark.parametrize("status", [500, 429, 401, 403])
async def test_actual_write_transport_failure_is_unknown_without_blind_retry(store, status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, text="private buyer text", headers={"retry-after": "120"})

    account = SafeAccount(
        "token=private-cookie",
        writes_enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    api = SimpleNamespace(account=account, lock=asyncio.Lock(), call=AsyncMock(return_value={"id": "chat"}))
    store.enqueue("test", "reply", "chat", {"text": "private buyer text"})
    worker = OutboxWorker(store, Executor(store, api, None))
    try:
        assert await worker.once()
        assert not await worker.once()
        assert len(calls) == 1
        assert calls[0].method == "POST"
        assert json.loads(calls[0].content)["operationName"] == "createChatMessage"
        assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
        assert "private buyer text" not in json.dumps(list(account.response_diagnostics))
    finally:
        account.close()


def test_late_response_cannot_overwrite_terminal_decision_and_is_deduplicated(store):
    ident = store.enqueue("test", "reply", "chat", {"text": "secret"})
    action = store.claim()
    store.mark_started(action)
    store.execute("UPDATE outbox SET lease_until=0")
    store.recover()
    store.execute(
        "UPDATE outbox SET status='PERMANENT_FAILED',error_code='MANUALLY_STOPPED_UNCERTAIN' WHERE id=?",
        (ident,),
    )
    worker = OutboxWorker(store, None)
    worker.finish(
        ident,
        action["lease_token"],
        "SUCCEEDED",
        {"message_id": "ack", "text": "secret", "nested": {"secret": "secret"}},
    )
    worker.finish(ident, action["lease_token"], "SUCCEEDED", {"message_id": "ack"})
    assert store.rows("SELECT status,error_code FROM outbox") == [
        {"status": "PERMANENT_FAILED", "error_code": "MANUALLY_STOPPED_UNCERTAIN"}
    ]
    rows = store.rows("SELECT * FROM outbox_late_results")
    assert len(rows) == 1
    assert json.loads(rows[0]["result"]) == {"message_id": "ack"}
    assert "secret" not in json.dumps(rows)
    assert len(store.rows("SELECT * FROM audit_log WHERE action='LATE_OUTBOX_RESULT'")) == 1


def test_unexpected_finish_conflict_is_not_hidden_as_a_late_response(store, monkeypatch):
    ident = store.enqueue("test", "reply", "chat", {})
    action = store.claim()

    def fail(*args, **kwargs):
        raise Conflict("Injected failure with still-owned lease")

    monkeypatch.setattr(store, "finish", fail)
    with pytest.raises(Conflict):
        OutboxWorker(store, None).finish(ident, action["lease_token"], "SUCCEEDED", {"message_id": "ack"})
    assert not store.rows("SELECT * FROM outbox_late_results")


async def test_late_receipt_ui_exposes_id_without_requeue_or_api_call(store):
    ident = store.enqueue("test", "reply", "chat", {"text": "secret"})
    action = store.claim()
    store.mark_started(action)
    store.execute("UPDATE outbox SET lease_until=0")
    store.recover()
    OutboxWorker(store, None).finish(ident, action["lease_token"], "SUCCEEDED", {"message_id": "<ack>"})
    api, session = CatalogAPI(), TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        await ui.click("ops:show:" + ident)
        text = session.calls[-1].text
        assert "Поздний ответ исполнителя" in text and "&lt;ack&gt;" in text
        assert "Основной статус не изменён" in text
        assert "secret" not in text
        assert not api.calls
        assert len(store.rows("SELECT * FROM outbox")) == 1
        assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_concurrent_publish_button_confirmations_dispatch_one_mutation(store):
    api, session = FakeAPI(), TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("publish:controlled-draft")
        nonce = (await state.get_data())["launch"]["nonce"]
        await ui.click("ln:" + nonce + ":check")
        nonce = (await state.get_data())["launch"]["nonce"]
        await asyncio.gather(ui.click("ln:" + nonce + ":commit"), ui.click("ln:" + nonce + ":commit"))
        assert len(store.rows("SELECT * FROM outbox WHERE kind='publish'")) == 1
        worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
        assert await worker.once() and not await worker.once()
        assert api.account.calls == ["publishItem"]
        assert store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
    finally:
        await dp.storage.close()
        await bot.session.close()


@pytest.mark.parametrize("stage", ["before_send", "after_send"])
async def test_abrupt_process_exit_preserves_reservation_and_prevents_resend(tmp_path, stage):
    path, receipt = tmp_path / "crash.sqlite", tmp_path / "mock_remote_receipt.txt"
    script = """
import os, sys
from pathlib import Path
from app.database.store import Store
from app.services.orders import Orders
s = Store(sys.argv[1])
orders = Orders(s)
orders.ingest({"id":"order","status":"PAID","direction":"OUT","has_problem":False,"is_automated":False,"item":{"id":"item"},"chat":{"id":"chat"}})
orders.add_stock("item", ["synthetic-stock-code"])
orders.reserve("order")
action=s.claim()
if sys.argv[3] == "after_send":
    s.mark_started(action)
    # A mock external system's durable receipt, independent of the application DB.
    with Path(sys.argv[2]).open("w", encoding="utf-8") as remote:
        remote.write("mock-ack")
        remote.flush()
        os.fsync(remote.fileno())
s.execute("UPDATE outbox SET lease_until=0")
os._exit(23)  # No finally, connection close or graceful worker cancellation.
"""
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(path), str(receipt), stage],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert process.returncode == 23
    reopened = Store(path)
    sender = AsyncMock()
    try:
        assert reopened.rows("SELECT status FROM outbox") == [{"status": "PROCESSING"}]
        assert reopened.rows("SELECT status FROM inventory") == [{"status": "RESERVED"}]
        assert reopened.recover() == 1
        assert reopened.rows("SELECT status FROM outbox") == [
            {"status": "UNCERTAIN" if stage == "after_send" else "RETRYABLE_FAILED"}
        ]
        assert reopened.rows("SELECT status FROM inventory") == [
            {"status": "QUARANTINED" if stage == "after_send" else "RESERVED"}
        ]
        assert len(reopened.rows("SELECT * FROM reservations")) == 1
        assert not await OutboxWorker(reopened, sender).once()
        sender.assert_not_called()
        assert receipt.exists() == (stage == "after_send")
        assert len(reopened.rows("SELECT * FROM inbox WHERE kind='operation_uncertain'")) == (
            1 if stage == "after_send" else 0
        )
    finally:
        reopened.close()
