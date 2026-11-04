"""Lost response allows observation, not invented attribution or another write."""

import asyncio
import threading
from copy import deepcopy

import pytest

from app.database.store import Store
from app.playerok.adapter import APIError
from app.services.edit_confirmation import EditConfirmation
from app.workers.outbox import Executor, OutboxWorker
from tests.test_operations import FakeAPI


@pytest.mark.parametrize("fault", ["lost", "unavailable", "expired"])
async def test_lost_edit_response_reads_only_with_live_lease_and_stays_unattributed(tmp_path, fault):
    store, api = Store(tmp_path / "lost.sqlite"), FakeAPI()
    item = {"id": "item", "user": {"id": "seller"}, "name": "Original title", "price": 100}
    reads, writes = [], []

    async def read(method, **kwargs):
        assert method == "get_item"
        reads.append(method)
        if writes and fault == "unavailable":
            raise APIError("NETWORK_ERROR")
        return deepcopy(item)

    def update(**kwargs):
        writes.append(kwargs)
        item["name"] = kwargs["name"]
        if fault == "expired":
            store.execute("UPDATE outbox SET lease_until=0")
        raise TimeoutError("test transport applied mutation but lost response")

    api.call, api.account.update_item = read, update
    ident = store.enqueue("lost", "edit", "item", {"name": "Updated product"})
    worker = OutboxWorker(store, Executor(store, api, None))
    try:
        await worker.once()
        assert len(writes) == 1
        # One preflight read plus the new bounded four-read visibility window.
        assert len(reads) == (1 if fault == "expired" else 5 if fault == "unavailable" else 2)
        proof = store.setting(EditConfirmation.key(ident))
        if fault == "lost":
            assert proof["readback_only"] and not proof["confirmed"]
        assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
        assert not await worker.once() and len(writes) == 1
    finally:
        store.close()


async def test_cancelled_edit_late_response_remains_unknown_after_reopen(tmp_path):
    path = tmp_path / "cancel.sqlite"
    store, api = Store(path), FakeAPI()
    applied, release, finished = threading.Event(), threading.Event(), threading.Event()
    item = {"id": "item", "user": {"id": "seller"}, "name": "Original title", "price": 100}
    writes = []

    async def read(method, **kwargs):
        assert method == "get_item"
        return deepcopy(item)

    def update(**kwargs):
        writes.append(kwargs)
        item["name"] = kwargs["name"]
        applied.set()
        release.wait()
        finished.set()
        return deepcopy(item)

    api.call, api.account.update_item = read, update
    store.enqueue("cancel", "edit", "item", {"name": "Updated product"})
    worker = OutboxWorker(store, Executor(store, api, None))
    task = asyncio.create_task(worker.once())
    try:
        async with asyncio.timeout(5):
            while not applied.is_set():
                await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        async with asyncio.timeout(5):
            while not finished.is_set():
                await asyncio.sleep(0)
        # Allow the executor's late-result callback to observe the cancelled call.
        await asyncio.sleep(0)
        assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
        assert not await worker.once() and len(writes) == 1
        store.close()
        store = Store(path)
        assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
        assert not await OutboxWorker(store, Executor(store, api, None)).once()
        assert len(writes) == 1
    finally:
        release.set()
        store.close()
