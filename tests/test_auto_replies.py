import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, Store
from app.playerok.adapter import APIError
from app.services.auto_replies import AutoReplies
from app.services.orders import Orders
from app.telegram.storage import SQLiteStorage
from app.workers.auto_replies import AutoReplyWorker
from app.workers.outbox import Executor, OutboxWorker
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


class Account:
    id, writes_enabled, automation_enabled = "seller", True, True

    def __init__(self):
        self.sent, self.error = [], None

    @contextmanager
    def permit(self, operation):
        assert operation == "createChatMessage"
        yield

    def send_message(self, **kwargs):
        self.sent.append(kwargs)
        if self.error:
            raise self.error
        return {"id": "ack"}


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "auto.sqlite")
    store.claim_first_owner(7)
    store.set_setting("playerok:account_id", "seller")
    store.set_setting("sync:health", {"status": "OK"})
    messages = []

    async def call(method, **kwargs):
        if method == "get_chat":
            return {
                "id": "chat",
                "type": "PM",
                "is_texting_allowed": True,
                "users": [{"id": "seller"}, {"id": "buyer"}],
            }
        if method == "get_chat_messages":
            return {"messages": messages, "page_info": {"has_next_page": False}}
        raise AssertionError(method)

    api = SimpleNamespace(account=Account(), lock=asyncio.Lock(), call=AsyncMock(side_effect=call))
    yield store, api, messages, AutoReplies(store, api)
    store.close()


def enable(service):
    return service.save(
        7, {"trigger_text": " Привет ", "response_text": "Здравствуйте!", "enabled": True}, armed=True
    )


def incoming(store, messages, ident="buyer-message", **changes):
    message = {
        "id": ident,
        "text": "ПРИВЕТ",
        "created_at": datetime.now(UTC).isoformat(),
        "user": {"id": "buyer", "role": "USER"},
        "event": None,
        "is_auto_response": False,
        "images": [],
        **changes,
    }
    messages[:] = [message]
    Orders(store).ingest_message("chat", message)
    return message


async def test_future_message_one_durable_reply_and_duplicate_poll_dedup(setup):
    store, api, messages, service = setup
    enable(service)
    incoming(store, messages)
    assert await AutoReplyWorker(store, api).once() == 1
    assert not api.account.sent
    assert await AutoReplyWorker(store, api).once() == 0
    worker = OutboxWorker(store, Executor(store, api, None))
    assert await worker.once() and not await worker.once()
    assert api.account.sent == [{"chat_id": "chat", "text": "Здравствуйте!"}]
    assert store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
    Orders(store).ingest_message("chat", messages[0])
    assert await AutoReplyWorker(store, api).once() == 0
    assert "Здравствуйте" not in str(store.rows("SELECT * FROM audit_log"))


@pytest.mark.parametrize(
    "changes",
    [
        {"created_at": "2024-01-01T00:00:00Z"},
        {"created_at": None},
        {"created_at": "2026-10-30T10:00:00"},
        {"user": {"id": "seller", "role": "USER"}},
        {"user": {"id": "buyer", "role": "ADMIN"}},
        {"is_auto_response": True},
        {"is_auto_response": None},
        {"event": "NEW_DEAL"},
        {"deleted_at": "deleted"},
        {"images": [{"id": "photo"}]},
        {"text": "привет и другая фраза"},
    ],
)
async def test_old_self_system_image_or_nonexact_text_never_auto_replied(setup, changes):
    store, api, messages, service = setup
    enable(service)
    incoming(store, messages, **changes)
    await AutoReplyWorker(store, api).once()
    assert not store.rows("SELECT * FROM outbox")
    assert not api.account.sent


async def test_runtime_baseline_and_explicit_arming_default_off(setup):
    store, api, messages, service = setup
    ident = service.save(7, {"trigger_text": "привет", "response_text": "ответ"})
    assert not service.get(7, ident)["enabled"]
    with pytest.raises(Conflict):
        service.save(
            7, {"trigger_text": "привет", "response_text": "ответ", "enabled": True}, ident=ident, revision=1
        )
    service.save(
        7,
        {"trigger_text": "привет", "response_text": "ответ", "enabled": True},
        ident=ident,
        revision=1,
        armed=True,
    )
    incoming(store, messages)
    api.account.automation_enabled = False
    assert await AutoReplyWorker(store, api).once() == 0
    api.account.automation_enabled = True
    store.set_setting("sync:health", {"status": "BACKFILL"})
    assert await AutoReplyWorker(store, api).once() == 0
    assert not store.rows("SELECT * FROM outbox")


async def test_message_before_enable_cutoff_never_replayed_on_enable(setup):
    store, api, messages, service = setup
    incoming(store, messages)
    enable(service)
    await AutoReplyWorker(store, api).once()
    assert not store.rows("SELECT * FROM outbox")


async def test_same_clock_tick_distinguishes_old_inbox_from_future_message(setup, monkeypatch):
    store, api, messages, service = setup
    fixed = datetime.now(UTC).isoformat()
    monkeypatch.setattr("app.services.auto_replies.now", lambda: fixed)
    monkeypatch.setattr("app.services.orders.now", lambda: fixed)
    incoming(store, messages, "old", created_at=fixed)
    enable(service)
    incoming(store, messages, "new", created_at=fixed)
    await AutoReplyWorker(store, api).once()
    assert store.rows("SELECT message_id,status FROM auto_reply_events ORDER BY message_id") == [
        {"message_id": "new", "status": "QUEUED"},
        {"message_id": "old", "status": "SKIPPED"},
    ]
    assert len(store.rows("SELECT * FROM outbox")) == 1


@pytest.mark.parametrize("change", ["policy", "runtime", "edit", "reply"])
async def test_stop_while_waiting_api_lock_blocks_auto_send(setup, change):
    store, api, messages, service = setup
    enable(service)
    original_message = incoming(store, messages)
    await AutoReplyWorker(store, api).once()
    checked = asyncio.Event()
    original = api.call.side_effect

    async def call(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_chat_messages":
            checked.set()
        return result

    api.call.side_effect = call
    await api.lock.acquire()
    task = asyncio.create_task(OutboxWorker(store, Executor(store, api, None)).once())
    try:
        await asyncio.wait_for(checked.wait(), 2)
        if change == "policy":
            service.stop(7)
        elif change == "runtime":
            api.account.automation_enabled = False
        elif change == "edit":
            Orders(store).ingest_message("chat", {**original_message, "text": "changed"})
        else:
            incoming(store, messages, "seller-reply", user={"id": "seller", "role": "USER"})
    finally:
        api.lock.release()
    await task
    assert not api.account.sent


async def test_newer_remote_message_and_support_chat_prevent_queue(setup):
    store, api, messages, service = setup
    enable(service)
    message = incoming(store, messages)
    messages.append(
        {
            **message,
            "id": "newer",
            "user": {"id": "seller", "role": "USER"},
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    await AutoReplyWorker(store, api).once()
    assert store.rows("SELECT status FROM auto_reply_events") == [{"status": "BLOCKED"}]
    assert not store.rows("SELECT * FROM outbox")


async def test_unknown_send_never_repeated_and_blocks_chat(setup):
    store, api, messages, service = setup
    enable(service)
    incoming(store, messages)
    await AutoReplyWorker(store, api).once()
    api.account.error = APIError("NETWORK_ERROR", uncertain=True)
    await OutboxWorker(store, Executor(store, api, None)).once()
    assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
    incoming(store, messages, "second")
    await AutoReplyWorker(store, api).once()
    assert len(store.rows("SELECT * FROM outbox")) == 1
    assert len(api.account.sent) == 1


async def test_parallel_workers_register_one_reply_and_restart_cannot_duplicate(setup):
    store, api, messages, service = setup
    enable(service)
    incoming(store, messages)
    await asyncio.gather(AutoReplyWorker(store, api).once(), AutoReplyWorker(store, api).once())
    assert len(store.rows("SELECT * FROM outbox")) == 1
    other = Store(store.path)
    try:
        assert await AutoReplyWorker(other, api).once() == 0
        await OutboxWorker(other, Executor(other, api, None)).once()
        assert len(api.account.sent) == 1
    finally:
        other.close()


async def test_read_rate_limit_stops_batch_and_backoff_survives_worker_restart(setup):
    store, api, messages, service = setup
    enable(service)
    incoming(store, messages)
    api.call.side_effect = APIError("RATE_LIMITED", retry_after="120")
    await AutoReplyWorker(store, api).once()
    assert api.call.await_count == 1
    assert await AutoReplyWorker(store, api).once() == 0
    assert api.call.await_count == 1
    assert store.setting("auto_replies:health")["status"] == "BACKOFF"
    assert not store.rows("SELECT * FROM outbox")


async def test_pending_delivery_in_same_chat_blocks_auto_reply(setup):
    store, api, messages, service = setup
    enable(service)
    incoming(store, messages)
    Orders(store).ingest(
        {
            "has_problem": False,
            "is_automated": False,
            "id": "order",
            "direction": "OUT",
            "status": "PAID",
            "item": {"id": "item", "user": {"id": "seller"}},
            "chat": {"id": "chat"},
        }
    )
    Orders(store).add_stock("item", ["private code"])
    Orders(store).reserve("order")
    await AutoReplyWorker(store, api).once()
    assert len(store.rows("SELECT * FROM outbox")) == 1
    assert store.rows("SELECT status FROM auto_reply_events")[0]["status"] == "BLOCKED"
    assert not api.account.sent


def test_moscow_day_limits_count_old_versions_and_shared_chat_cooldown(setup):
    store, _api, _messages, service = setup
    ident = enable(service)
    rule = service.get(7, ident)
    rule["daily_limit"] = 1
    at = datetime(2026, 10, 30, 21, tzinfo=UTC)  # Midnight Oct 31 in Moscow.
    store.execute(
        "INSERT INTO auto_reply_events VALUES('seller','old','different',?,0,'QUEUED',NULL,NULL,?)",
        (ident, (at - timedelta(seconds=1)).isoformat()),
    )
    service.limits(rule, "chat", at)
    store.execute(
        "INSERT INTO auto_reply_events VALUES('seller','today','different',?,0,'QUEUED',NULL,NULL,?)",
        (ident, at.isoformat()),
    )
    with pytest.raises(Conflict, match="лимит"):
        service.limits(rule, "chat", at)
    rule["daily_limit"] = 100
    store.execute("UPDATE auto_reply_events SET chat_id='chat' WHERE message_id='today'")
    with pytest.raises(Conflict, match="Пауза"):
        service.limits(rule, "chat", at)


@pytest.mark.parametrize("mode", ["support", "foreign", "closed", "changed", "missing"])
async def test_fresh_chat_or_message_mismatch_never_sends(setup, mode):
    store, api, messages, service = setup
    enable(service)
    incoming(store, messages)
    original = api.call.side_effect

    async def call(method, **kwargs):
        value = await original(method, **kwargs)
        if method == "get_chat":
            value = dict(value)
            if mode == "support":
                value["type"] = "SUPPORT"
            elif mode == "foreign":
                value["users"] = [{"id": "different"}, {"id": "buyer"}]
            elif mode == "closed":
                value["is_texting_allowed"] = False
        elif mode == "changed":
            value = {**value, "messages": [{**messages[0], "text": "different"}]}
        elif mode == "missing":
            value = {**value, "messages": []}
        return value

    api.call.side_effect = call
    await AutoReplyWorker(store, api).once()
    assert not store.rows("SELECT * FROM outbox")
    assert not api.account.sent


def test_rule_owner_cas_limits_duplicate_phrase_and_validation(setup):
    _store, _api, _messages, service = setup
    ident = enable(service)
    with pytest.raises(Conflict):
        service.save(7, {"trigger_text": "привет", "response_text": "other"})
    with pytest.raises(Conflict):
        service.save(8, {"trigger_text": "other", "response_text": "other"})
    with pytest.raises(Conflict):
        service.save(7, {"trigger_text": "other", "response_text": "other"}, ident=ident, revision=0)
    with pytest.raises(ValueError):
        service.save(7, {"trigger_text": "other", "response_text": "other", "cooldown": 1})
    with pytest.raises(Conflict):
        service.delete(7, ident, 1)
    service.stop(7)
    service.delete(7, ident, 2)
    assert not service.rules(7)


async def test_dispatcher_create_disabled_then_separate_arming_restart_and_no_sending(setup):
    store, api, _messages, service = setup
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("ar:new")
        for value in ["Привет", "<Здравствуйте>"]:
            await ui.message(value)
        rule = service.rules(7)[0]
        assert not rule["enabled"]
        await ui.click("ar:toggle:" + rule["id"])
        nonce = (await state.get_data())["auto_reply_nonce"]
        await dp.storage.close()
        dp = dispatcher(store, api)
        ui = Interaction(bot, dp)
        await ui.click("ar:commit:" + nonce)
        await ui.click("ar:commit:" + nonce)
        assert "устарело" in session.calls[-1].text
        assert service.rules(7)[0]["enabled"]
        assert not api.account.sent and not store.rows("SELECT * FROM outbox")
        await ui.click("ar:show:" + rule["id"])
        assert "&lt;Здравствуйте&gt;" in session.calls[-1].text
        await ui.stop("ar:stop")
        assert not service.rules(7)[0]["enabled"]
    finally:
        await dp.storage.close()
        await bot.session.close()
