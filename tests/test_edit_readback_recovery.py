"""Production Outbox; only Playerok transport responses are controlled."""

import sqlite3
from copy import deepcopy

import pytest

from app.database.store import Conflict, Store
from app.playerok.adapter import APIError
from app.services.reconciliation import Reconciliation, signature
from app.workers.outbox import Executor, OutboxWorker
from tests.test_operations import FakeAPI


@pytest.mark.parametrize("reply", ["partial", "stale", "delayed", "fourth-read"])
async def test_incomplete_mutation_is_confirmed_by_fresh_read_without_repeating_write(tmp_path, reply):
    store = Store(tmp_path / "readback.sqlite")
    api = FakeAPI()
    changes = {"name": "Updated product name", "description": "New exact description", "price": 149}
    item = {
        "id": "item",
        "user": {"id": "seller"},
        "name": "Original product",
        "description": "Old",
        "price": 100,
    }
    calls, reads = [], []
    original = deepcopy(item)

    async def read(method, **kwargs):
        assert method == "get_item"
        reads.append(method)
        if reply == "delayed" and len(reads) == 2:
            return deepcopy(original)
        if reply == "fourth-read" and 2 <= len(reads) <= 4:
            return deepcopy(original)
        return deepcopy(item)

    def update(**kwargs):
        calls.append(kwargs)
        item.update(changes)
        return {"id": "item"} if reply in {"partial", "delayed", "fourth-read"} else original

    api.call, api.account.update_item = read, update
    ident = store.enqueue("edit", "edit", "item", changes)
    worker = OutboxWorker(store, Executor(store, api, None))
    try:
        await worker.once()
        assert store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "SUCCEEDED"}]
        assert len(calls) == 1 and len(reads) >= 2
        assert not await worker.once()
    finally:
        store.close()


@pytest.mark.parametrize("partial", [False, True])
async def test_restart_after_confirmed_receipt_commits_readonly_and_never_mutates_again(
    tmp_path, monkeypatch, partial
):
    path = tmp_path / "restart.sqlite"
    store = Store(path)
    store.claim_first_owner(7)
    store.set_setting("playerok:account_id", "seller")
    api, writes = FakeAPI(), []
    item = {"id": "item", "user": {"id": "seller"}, "name": "Old product", "price": 100}

    async def read(method, **kwargs):
        assert method == "get_item"
        return deepcopy(item)

    def update(**kwargs):
        writes.append(kwargs)
        item["name"] = kwargs["name"]
        return {"id": "item"} if partial else deepcopy(item)

    api.call, api.account.update_item = read, update
    ident = store.enqueue("edit", "edit", "item", {"name": "New product"}, actor=7)
    real_finish = store.finish

    def disk_failure(*args, **kwargs):
        raise sqlite3.OperationalError("isolated crash after receipt")

    monkeypatch.setattr(store, "finish", disk_failure)
    with pytest.raises(sqlite3.OperationalError):
        await OutboxWorker(store, Executor(store, api, None)).once()
    monkeypatch.setattr(store, "finish", real_finish)
    store.execute("UPDATE outbox SET lease_until=0")
    store.close()
    store = Store(path)
    try:
        assert store.recover() == 1
        service = Reconciliation(store, api)
        before = service.action(7, ident)
        result = await service.resolve(
            7, ident, signature(before), "CONFIRM_READ_STATE", "Проверка сохранённой квитанции"
        )
        assert result["remote_id"] == "item"
        assert service.action(7, ident)["status"] == "SUCCEEDED"
        assert len(writes) == 1
        with pytest.raises(Conflict):
            await service.resolve(
                7, ident, signature(before), "CONFIRM_READ_STATE", "Повторная проверка записи"
            )
        assert not await OutboxWorker(store, Executor(store, api, None)).once()
    finally:
        store.close()


async def test_old_unknown_equal_state_does_not_invent_receipt_or_repeat(tmp_path):
    store = Store(tmp_path / "old.sqlite")
    store.claim_first_owner(7)
    store.set_setting("playerok:account_id", "seller")
    api = FakeAPI()
    reads = []

    async def read(method, **kwargs):
        reads.append(method)
        return {"id": "item", "user": {"id": "seller"}, "name": "New product"}

    api.call = read
    ident = store.enqueue("old", "edit", "item", {"name": "New product"}, actor=7)
    claimed = store.claim()
    store.mark_started(claimed)
    store.finish(ident, claimed["lease_token"], "UNCERTAIN", error="EDIT_NOT_CONFIRMED")
    try:
        service = Reconciliation(store, api)
        before = service.action(7, ident)
        proof = await service.evidence(7, ident)
        assert proof["decision"] == "CURRENT_STATE_ONLY" and proof["matches"]
        assert reads == ["get_item"]
        with pytest.raises(Conflict):
            await service.resolve(
                7, ident, signature(before), "CONFIRM_READ_STATE", "Внешне значения совпадают"
            )
        assert service.action(7, ident)["status"] == "UNCERTAIN"
        assert not api.account.calls
    finally:
        store.close()


@pytest.mark.parametrize("fault", ["old", "offline", "wrong-owner", "wrong-id"])
async def test_failed_readback_remains_unknown_and_never_repeats_mutation(tmp_path, fault):
    store = Store(tmp_path / "unknown.sqlite")
    api = FakeAPI()
    original = api.call
    writes = []

    async def read(method, **kwargs):
        if writes:
            if fault == "offline":
                raise APIError("NETWORK_ERROR")
            value = {"id": "item", "user": {"id": "seller"}, "name": "Updated product name"}
            if fault == "old":
                value["name"] = "Original product"
            elif fault == "wrong-owner":
                value["user"]["id"] = "other"
            else:
                value["id"] = "other"
            return value
        return await original(method, **kwargs)

    def update(**kwargs):
        writes.append(kwargs)
        return {"id": "item"}

    api.call, api.account.update_item = read, update
    store.enqueue("edit", "edit", "item", {"name": "Updated product name"})
    worker = OutboxWorker(store, Executor(store, api, None))
    try:
        await worker.once()
        assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
        assert len(writes) == 1 and not await worker.once()
        from app.services.edit_confirmation import EditConfirmation
        from app.telegram.presentation import edit_readback_note

        proof = store.setting(EditConfirmation.key(store.rows("SELECT id FROM outbox")[0]["id"]))
        assert proof["confirmed"] is False
        assert proof["last_readback"]["attempts"] == 4
        expected = {"old": "name", "offline": "network", "wrong-owner": "owner", "wrong-id": "id"}
        assert expected[fault] in proof["last_readback"]["fields"]
        assert "не подтвердила" in edit_readback_note(proof)
        assert "Updated product name" not in edit_readback_note(proof)
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["add", "replace", "remove"])
async def test_partial_photo_mutation_readback_checks_kept_removed_and_new_ids(tmp_path, monkeypatch, mode):
    from app.services.attachments import Attachments

    monkeypatch.setattr("app.workers.outbox.Attachments", lambda: Attachments(tmp_path))
    store, api = Store(tmp_path / "photos.sqlite"), FakeAPI()
    photo = tmp_path / "isolated.jpg"
    photo.write_bytes(b"isolated transport fixture")
    old = ["old-one", "old-two", "old-three"]
    keep = old if mode == "add" else old[:2]
    item = {"id": "item", "user": {"id": "seller"}, "attachments": [{"id": i} for i in old]}
    writes = []

    async def read(method, **kwargs):
        assert method == "get_item"
        return deepcopy(item)

    def update(**changes):
        writes.append(changes)
        item["attachments"] = [{"id": i} for i in keep] + ([{"id": "new-image"}] if mode != "remove" else [])
        return {"id": "item"}

    api.call, api.account.update_item = read, update
    store.enqueue(
        "photos",
        "edit",
        "item",
        {
            "keep_attachment_ids": keep,
            "replace_attachment_ids": old,
            "add_photos": [] if mode == "remove" else [str(photo)],
        },
    )
    try:
        await OutboxWorker(store, Executor(store, api, None)).once()
        assert store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
        assert len(writes) == 1
        assert set(writes[0].get("remove_attachments", [])) == set(old) - set(keep)
    finally:
        store.close()


@pytest.mark.parametrize("fault", ["category", "obtaining_type", "description", "lease", "lost-response"])
async def test_concurrent_contract_change_or_lost_authority_cannot_be_confirmed(tmp_path, fault):
    store, api = Store(tmp_path / "fenced.sqlite"), FakeAPI()
    item = {
        "id": "item",
        "user": {"id": "seller"},
        "name": "Original title",
        "description": "Original description",
        "category": {"id": "original"},
        "obtaining_type": {"id": "original"},
    }
    writes = []

    async def read(method, **kwargs):
        assert method == "get_item"
        if writes and fault == "lease":
            store.execute("UPDATE outbox SET lease_until=0")
        return deepcopy(item)

    def update(**changes):
        writes.append(changes)
        item["name"] = changes["name"]
        if fault in {"category", "obtaining_type"}:
            item[fault] = {"id": "other"}
        elif fault == "description":
            item["description"] = "Changed by another client"
        elif fault == "lost-response":
            raise TimeoutError("transport lost after write")
        return {"id": "item"}

    api.call, api.account.update_item = read, update
    store.enqueue("edit", "edit", "item", {"name": "Updated product title"})
    worker = OutboxWorker(store, Executor(store, api, None))
    try:
        await worker.once()
        assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
        assert not await worker.once() and len(writes) == 1
    finally:
        store.close()
