"""Only free pre-send reads may retry; durable replies never replay."""

import asyncio
import time

import pytest

from app.playerok.adapter import APIError
from app.workers.auto_replies import AutoReplyWorker
from app.workers.outbox import Executor, OutboxWorker
from tests.test_auto_replies import enable, incoming
from tests.test_auto_replies import setup as fixture


@pytest.fixture
def shop(tmp_path):
    yield from fixture.__wrapped__(tmp_path)


async def test_transient_chat_read_recovers_after_restart_without_blind_send(shop, monkeypatch):
    store, api, messages, service = shop
    enable(service)
    incoming(store, messages)
    read = api.call.side_effect
    api.call.side_effect = APIError("HTTP_503")
    await AutoReplyWorker(store, api).once()
    assert not store.rows("SELECT * FROM outbox") and not api.account.sent
    assert store.setting("auto_replies:health")["status"] == "ERROR"
    api.call.side_effect = read
    assert await AutoReplyWorker(store, api).once() == 0
    at = time.time()
    monkeypatch.setattr("app.workers.auto_replies.time.time", lambda: at + 121)
    await AutoReplyWorker(store, api).once()
    await OutboxWorker(store, Executor(store, api, None)).once()
    assert len(api.account.sent) == 1
    assert store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
    assert not await OutboxWorker(store, Executor(store, api, None)).once()


async def test_free_read_retries_are_bounded_and_cannot_starve_forever(shop, monkeypatch):
    store, api, messages, service = shop
    enable(service)
    incoming(store, messages)
    api.call.side_effect = APIError("HTTP_503")
    at = [time.time()]
    monkeypatch.setattr("app.workers.auto_replies.time.time", lambda: at[0])
    for _ in range(6):
        await AutoReplyWorker(store, api).once()
        at[0] += 181
    assert api.call.await_count == 3
    assert store.rows("SELECT status FROM auto_reply_events") == [{"status": "BLOCKED"}]
    assert not store.rows("SELECT * FROM outbox") and not api.account.sent


async def test_dialog_continued_during_read_backoff_is_not_answered(shop, monkeypatch):
    store, api, messages, service = shop
    enable(service)
    old = incoming(store, messages)
    read = api.call.side_effect
    api.call.side_effect = APIError("NETWORK_ERROR")
    await AutoReplyWorker(store, api).once()
    api.call.side_effect = read
    messages.append({**old, "id": "later", "user": {"id": "seller", "role": "USER"}})
    at = time.time()
    monkeypatch.setattr("app.workers.auto_replies.time.time", lambda: at + 121)
    await AutoReplyWorker(store, api).once()
    assert not store.rows("SELECT * FROM outbox") and not api.account.sent
    assert store.rows("SELECT status FROM auto_reply_events") == [{"status": "BLOCKED"}]


async def test_concurrent_success_clears_an_obsolete_free_read_backoff(shop, monkeypatch):
    store, api, messages, service = shop
    enable(service)
    incoming(store, messages)
    read = api.call.side_effect
    api.call.side_effect = APIError("HTTP_503")
    await AutoReplyWorker(store, api).once()
    at = time.time()
    monkeypatch.setattr("app.workers.auto_replies.time.time", lambda: at + 121)
    entered, release = asyncio.Event(), asyncio.Event()

    async def concurrent_read(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
            raise APIError("HTTP_503")
        return await read(*args, **kwargs)

    api.call.side_effect = concurrent_read
    first = asyncio.create_task(AutoReplyWorker(store, api).once())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await AutoReplyWorker(store, api).once()
    finally:
        release.set()
        await first
    await AutoReplyWorker(store, api).once()
    assert not store.rows("SELECT key FROM settings WHERE key LIKE 'auto_replies:check:%'")
    assert store.setting("auto_replies:health")["status"] == "ACTIVE"
    await OutboxWorker(store, Executor(store, api, None)).once()
    assert len(api.account.sent) == 1
