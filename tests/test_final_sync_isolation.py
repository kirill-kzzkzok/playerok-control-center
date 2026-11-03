"""Chat degradation stays scoped to its own conversation."""

import asyncio

import pytest

from app.database.store import now
from app.services.orders import Orders
from app.workers.auto_replies import AutoReplyWorker
from app.workers.outbox import Executor, OutboxWorker
from app.workers.sync import Synchronizer
from tests.test_auto_replies import enable, incoming
from tests.test_auto_replies import setup as replies_fixture
from tests.test_final_publish_authority import launch as launch_fixture
from tests.test_listing_launch import configured


@pytest.fixture
def replies(tmp_path):
    yield from replies_fixture.__wrapped__(tmp_path)


@pytest.fixture
def launch(tmp_path):
    yield from launch_fixture.__wrapped__(tmp_path)


async def test_new_prepared_gift_launch_survives_only_unrelated_old_chat_error(launch):
    r = launch
    ident, _ = await configured(r)
    r.store.set_setting("sync:health", {"status": "ERROR", "errors": {"messages:old-chat": "HTTP_503"}})
    draft = r.launch.load(7, ident)
    ready = await r.launch.check(7, draft["payload"])
    assert ready["bundle"]
    assert not r.publications and not r.api.sent


@pytest.mark.parametrize("failure", ["old-chat", "same-chat", "orders", "unclassified"])
async def test_autoreply_worker_and_dispatch_use_own_chat_proof(replies, failure):
    store, api, messages, service = replies
    enable(service)
    incoming(store, messages)
    store.set_setting("orders:last_complete_sync", now())
    store.execute(
        "INSERT INTO sync_chats(id,payload,discovered_at,initial_complete,last_synced_at) VALUES(?,? ,?,1,?)",
        ("chat", "{}", now(), now()),
    )
    key = (
        "messages:chat"
        if failure == "same-chat"
        else "messages:old-chat"
        if failure == "old-chat"
        else failure
    )
    store.set_setting("sync:health", {"status": "ERROR", "errors": {key: "HTTP_503"}})
    await AutoReplyWorker(store, api).once()
    await OutboxWorker(store, Executor(store, api, None)).once()
    assert len(api.account.sent) == int(failure == "old-chat")
    if failure == "old-chat":
        assert store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
    assert store.setting("sync:health")["status"] == "ERROR"


async def test_old_chat_failure_does_not_inflate_real_polling_sleep(launch, monkeypatch):
    r = launch
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_deals":
            return {"deals": [], "page_info": {"has_next_page": False}}
        if method == "get_chats":
            return {"chats": [{"id": "old"}], "page_info": {"has_next_page": False}}
        if method == "get_chat_messages":
            from app.playerok.adapter import APIError

            raise APIError("HTTP_503")
        return await original(method, **kwargs)

    r.api.call = call
    sync = Synchronizer(r.store, r.api, Orders(r.store))
    stop = asyncio.Event()
    delays = []

    async def wait(tasks, *, timeout, return_when):
        delays.append(timeout)
        stop.set()
        return set(), set(tasks)

    monkeypatch.setattr("app.workers.sync.asyncio.wait", wait)
    monkeypatch.setattr("app.workers.sync.random.uniform", lambda *_: 0)
    await sync.run(stop)
    assert sync.health["errors"] == {"messages:old": "HTTP_503"}
    assert len(delays) == 1 and 25 <= delays[0] <= 31
    assert r.store.setting("orders:last_complete_sync")


async def test_many_degraded_chat_candidates_cannot_starve_one_healthy_reply(replies):
    store, api, messages, service = replies
    enable(service)
    for index in range(24):
        Orders(store).ingest_message("old", {"id": "old-" + str(index), "text": "привет"})
    incoming(store, messages)
    store.set_setting("orders:last_complete_sync", now())
    store.set_setting("sync:health", {"status": "ERROR", "errors": {"messages:old": "HTTP_503"}})
    store.execute(
        "INSERT INTO sync_chats(id,payload,discovered_at,initial_complete) VALUES('chat','{}',?,1)", (now(),)
    )
    await AutoReplyWorker(store, api).once()
    await OutboxWorker(store, Executor(store, api, None)).once()
    assert len(api.account.sent) == 1
    assert store.rows("SELECT message_id FROM auto_reply_events") == [{"message_id": "buyer-message"}]
