import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store
from app.telegram.listings_panel import ListingsPanel
from app.telegram.storage import SQLiteStorage


def test_panel_protection_uses_covering_indexes(tmp_path):
    store = Store(tmp_path / "large.sqlite")
    plans = []
    for table in ("notifications", "operation_receipts"):
        plan = store.rows(
            "EXPLAIN QUERY PLAN SELECT 1 FROM " + table + " WHERE owner_id=? AND telegram_message_id=?",
            (7, 50),
        )
        plans.extend(str(row) for row in plan)
    assert all("SEARCH" in row and "COVERING INDEX" in row for row in plans), plans
    store.close()


async def test_catalog_search_is_bounded_and_can_continue(tmp_path):
    from app.services.item_filters import refresh_due_catalog

    store = Store(tmp_path / "search.sqlite")
    store.set_setting("playerok:account_id", "seller")
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    reads = []

    async def call(method, **kwargs):
        reads.append(kwargs)
        return {
            "items": [{"id": str(len(reads)), "name": "match", "status": "APPROVED", "price": 10}],
            "page_info": {"has_next_page": len(reads) < 4, "end_cursor": str(len(reads))},
        }

    api = SimpleNamespace(account=SimpleNamespace(id="seller"), call=AsyncMock(side_effect=call))
    _page, cache, count = await ListingsPanel(api, store).search_page({"search": "match"}, state, None)
    assert len(reads) == 1
    assert not cache["complete"] and count == 1
    await ListingsPanel(api, store).search_page({"search": "match"}, state, None)
    assert len(reads) == 1
    for expected_reads in (2, 3, 4):
        await refresh_due_catalog(store, api)
        assert len(reads) == expected_reads
    _page, cache, count = await ListingsPanel(api, store).search_page({"search": "match"}, state, None)
    assert cache["complete"] and count == 4
    assert [read["after_cursor"] for read in reads] == [None, "1", "2", "3"]
    assert not store.rows("SELECT * FROM outbox")
    store.close()


async def test_backup_copy_does_not_hold_shared_ui_connection(tmp_path, monkeypatch):
    store = Store(tmp_path / "source.sqlite")
    store.set_setting("committed", True)
    started = threading.Event()
    original_connect = sqlite3.connect

    class Slow(sqlite3.Connection):
        def backup(self, *args, **kwargs):
            started.set()
            time.sleep(0.3)
            return super().backup(*args, **kwargs)

    # Deliberately slow source copies both before and after the implementation change.
    store.db.close()
    store.db = original_connect(store.path, check_same_thread=False, isolation_level=None, factory=Slow)
    store.db.row_factory = sqlite3.Row
    monkeypatch.setattr(
        sqlite3, "connect", lambda *args, **kwargs: original_connect(*args, **{**kwargs, "factory": Slow})
    )
    task = asyncio.create_task(asyncio.to_thread(store.backup, tmp_path / "backup.sqlite"))
    assert await asyncio.to_thread(started.wait, 2)
    before = time.perf_counter()
    assert store.setting("committed") is True
    elapsed = time.perf_counter() - before
    await task
    assert elapsed < 0.15, elapsed
    with original_connect(tmp_path / "backup.sqlite") as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    store.close()
