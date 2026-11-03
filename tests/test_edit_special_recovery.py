"""Special edit receipts finalize their existing linked journals atomically."""

import pytest

from app.services.products import Products
from app.services.reconciliation import Reconciliation, signature
from app.workers.outbox import Executor, OutboxWorker
from tests.test_code_migration import legacy as legacy_fixture
from tests.test_logical_products import logical as logical_fixture

legacy = legacy_fixture
logical = logical_fixture


async def test_operation_check_finalizes_code_migration_without_second_write(legacy, monkeypatch):
    store, api, migration = legacy
    ident = migration.queue(7, await migration.prepare(7))[0]
    api.lost_ack = True
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    service = Reconciliation(store, api)
    before = service.action(7, ident)
    assert before["status"] == "UNCERTAIN"
    import app.services.code_migration as module

    real_finish = module.finish_migration

    def fail(*args, **kwargs):
        raise RuntimeError("isolated journal failure")

    monkeypatch.setattr(module, "finish_migration", fail)
    with pytest.raises(RuntimeError):
        await service.resolve(7, ident, signature(before), "CONFIRM_READ_STATE", "Сверены все поля переноса")
    assert service.action(7, ident)["status"] == "UNCERTAIN"
    assert store.rows("SELECT state FROM logical_products") == [{"state": "BLOCKED"}]
    monkeypatch.setattr(module, "finish_migration", real_finish)
    await service.resolve(7, ident, signature(before), "CONFIRM_READ_STATE", "Сверены все поля переноса")
    assert service.action(7, ident)["status"] == "SUCCEEDED"
    assert store.rows("SELECT state FROM logical_products") == [{"state": "VERIFIED"}]
    assert store.rows("SELECT state FROM logical_code_migration_items") == [{"state": "VERIFIED"}]
    assert len(api.mutations) == 1


async def test_operation_check_finalizes_binding_but_does_not_arm(logical):
    store, api, binding = logical
    prepared = await binding.prepare(7, "lot", "fixed_text", {"text": "private unit"})
    request = binding.commit(7, prepared)
    api.lost_ack = True
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    service = Reconciliation(store, api)
    row = service.action(7, request["operation_id"])
    assert row["status"] == "UNCERTAIN"
    await service.resolve(
        7, row["id"], signature(row), "CONFIRM_READ_STATE", "Объявление и постоянный код сверены"
    )
    product = binding.listing("lot")
    assert product["state"] == "VERIFIED" and product["verified_at"] and not product["enabled"]
    assert not store.rows("SELECT * FROM delivery_policies")
    assert len(api.mutations) == 1
