import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.store import Store
from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.workers.sync import Synchronizer


def page(ids, cursor=None, collection="messages"):
    return {
        collection: [{"id": ident, "text": ident} for ident in ids],
        "page_info": {"has_next_page": cursor is not None, "end_cursor": cursor},
    }


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "sync.sqlite")
    yield value
    value.close()


async def test_bounded_resume_polls_new_head_and_deduplicates_after_restart(store):
    api = SimpleNamespace(
        call=AsyncMock(side_effect=[page(["old"], "a"), page(["new", "old"], "b"), page(["older"])])
    )
    orders = Orders(store)
    sync = Synchronizer(store, api, orders, pages_per_stream=1)
    ingest = lambda message: orders.ingest_message("chat", message)
    assert await sync.scan("messages:chat", "get_chat_messages", "messages", ingest) == (1, False)
    # Reconstruct the worker: progress comes exclusively from SQLite.
    sync = Synchronizer(store, api, orders, pages_per_stream=1)
    assert await sync.scan("messages:chat", "get_chat_messages", "messages", ingest) == (3, True)
    assert [call.kwargs["after_cursor"] for call in api.call.call_args_list] == [None, None, "a"]
    assert len(store.rows("SELECT * FROM inbox")) == 3
    assert store.setting("messages:chat:completed_at")


async def test_network_failure_retains_last_committed_checkpoint(store):
    api = SimpleNamespace(call=AsyncMock(side_effect=[page(["one"], "a"), APIError("NETWORK_ERROR")]))
    sync = Synchronizer(store, api, Orders(store), pages_per_stream=2)
    with pytest.raises(APIError, match="NETWORK_ERROR"):
        await sync.scan(
            "messages:chat", "get_chat_messages", "messages", lambda m: sync.orders.ingest_message("chat", m)
        )
    assert json.loads(store.rows("SELECT cursor FROM sync_cursors")[0]["cursor"])["after"] == "a"
    assert len(store.rows("SELECT * FROM inbox")) == 1


async def test_partial_page_commit_replays_safely_without_advancing_cursor(store):
    api = SimpleNamespace(call=AsyncMock(return_value=page(["one", "two"], "a")))
    sync = Synchronizer(store, api, Orders(store), pages_per_stream=1)

    def broken(message):
        if message["id"] == "two":
            raise RuntimeError("disk interruption")
        sync.orders.ingest_message("chat", message)

    with pytest.raises(RuntimeError):
        await sync.scan("messages:chat", "get_chat_messages", "messages", broken)
    assert not store.rows("SELECT * FROM sync_cursors")
    await sync.scan(
        "messages:chat", "get_chat_messages", "messages", lambda m: sync.orders.ingest_message("chat", m)
    )
    assert len(store.rows("SELECT * FROM inbox")) == 2


async def test_repeated_cursor_resets_pass_without_processing_bad_page(store):
    api = SimpleNamespace(call=AsyncMock(side_effect=[page(["one"], "a"), page(["two"], "a")]))
    sync = Synchronizer(store, api, Orders(store))
    with pytest.raises(APIError, match="INVALID_PAGINATION_CURSOR"):
        await sync.scan(
            "messages:chat", "get_chat_messages", "messages", lambda m: sync.orders.ingest_message("chat", m)
        )
    assert not store.rows("SELECT * FROM sync_cursors")
    assert [row["id"] for row in store.rows("SELECT id FROM chat_messages")] == ["one"]


async def test_failed_chat_does_not_starve_other_chats_and_backfill_is_visible(store):
    async def call(method, **kwargs):
        if method == "get_deals":
            return page([], collection="deals")
        if method == "get_chats":
            return page(["a", "b"], collection="chats")
        if kwargs["chat_id"] == "a":
            raise APIError("HTTP_403")
        return page(["message"])

    sync = Synchronizer(store, SimpleNamespace(call=call), Orders(store))
    await sync.once()
    assert sync.health["status"] == "ERROR"
    assert sync.health["chat_backlog"] == 1
    assert store.rows("SELECT chat_id FROM chat_messages") == [{"chat_id": "b"}]
    assert store.rows("SELECT error_code FROM sync_chats WHERE id='a'")[0]["error_code"] == "HTTP_403"


async def test_message_refresh_updates_payload_without_new_event(store):
    orders = Orders(store)
    assert orders.ingest_message("chat", {"id": "m", "text": "first"})
    assert not orders.ingest_message("chat", {"id": "m", "text": "edited", "is_read": True})
    assert json.loads(store.rows("SELECT payload FROM chat_messages")[0]["payload"])["text"] == "edited"
    assert len(store.rows("SELECT * FROM inbox")) == 1


async def test_round_robin_attempts_inaccessible_chat_without_starving_others(store):
    attempted = []

    async def call(method, **kwargs):
        if method == "get_deals":
            return page([], collection="deals")
        if method == "get_chats":
            return page(["a", "b", "c"], collection="chats")
        attempted.append(kwargs["chat_id"])
        if kwargs["chat_id"] == "a":
            raise APIError("HTTP_403")
        return page([])

    sync = Synchronizer(store, SimpleNamespace(call=call), Orders(store), chats_per_cycle=1)
    for _ in range(4):
        await sync.once()
    assert attempted == ["a", "b", "c", "a"]


async def test_cursor_survives_database_close_and_reopen(tmp_path):
    path = tmp_path / "restart.sqlite"
    store = Store(path)
    api = SimpleNamespace(call=AsyncMock(return_value=page(["one"], "a")))
    sync = Synchronizer(store, api, Orders(store), pages_per_stream=1)
    await sync.scan(
        "history", "get_chat_messages", "messages", lambda m: sync.orders.ingest_message("chat", m)
    )
    store.close()
    store = Store(path)
    try:
        api.call = AsyncMock(side_effect=[page(["one"], "a"), page(["two"])])
        sync = Synchronizer(store, api, Orders(store), pages_per_stream=1)
        await sync.scan(
            "history", "get_chat_messages", "messages", lambda m: sync.orders.ingest_message("chat", m)
        )
        assert api.call.call_args.kwargs["after_cursor"] == "a"
        assert len(store.rows("SELECT * FROM inbox")) == 2
    finally:
        store.close()
