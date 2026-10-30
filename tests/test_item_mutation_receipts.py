import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.database.store import Store
from app.playerok.adapter import SafeAccount
from app.services.products import Products
from app.workers.outbox import Executor, OutboxWorker
from tests.test_operations import FakeAPI


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "receipts.sqlite")
    yield value
    value.close()


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {},
        {"id": "item"},
        {"id": "item", "status": None},
        {"id": "other", "status": "APPROVED"},
        {"id": "item", "status": "DRAFT"},
        {"id": "item", "status": "DECLINED"},
        {"id": "item", "status": "BLOCKED"},
        {"id": "item", "status": "EXPIRED"},
        {"id": "item", "status": "SOLD"},
        {"id": "item", "status": "UNKNOWN"},
    ],
)
async def test_publish_incomplete_or_nonpublished_receipt_is_unknown_without_repeat(store, receipt):
    api = FakeAPI()
    sent = []

    def publish(**kwargs):
        sent.append(kwargs)
        return receipt

    api.account.publish_item = publish
    ident = store.enqueue(
        "publish", "publish", "item", {"priority_id": "free", "price": 100, "raw_price": 100}
    )
    worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
    assert await worker.once()
    assert not await worker.once()
    assert len(sent) == 1
    assert store.rows("SELECT status,error_code FROM outbox WHERE id=?", (ident,)) == [
        {"status": "UNCERTAIN", "error_code": "PUBLICATION_NOT_CONFIRMED"}
    ]
    assert store.rows("SELECT kind FROM inbox") == [{"kind": "operation_uncertain"}]


@pytest.mark.parametrize("status", ["APPROVED", "PENDING_APPROVAL", "PENDING_MODERATION"])
async def test_explicit_published_or_moderation_status_is_retained(store, status):
    api = FakeAPI()
    api.account.publish_item = lambda **kwargs: {"id": "item", "status": status}
    store.enqueue("publish", "publish", "item", {"priority_id": "free", "price": 100, "raw_price": 100})
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    row = store.rows("SELECT status,result FROM outbox")[0]
    assert row["status"] == "SUCCEEDED"
    assert json.loads(row["result"])["status"] == status


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        False,
        True,
        {},
        {"id": "item"},
        {"id": "other", "deletedAt": "2026-10-30T12:00:00Z"},
        {"id": "item", "deletedAt": None},
        {"id": "item", "deletedAt": "invalid"},
        {"id": "item", "deletedAt": "2026-10-30T12:00:00"},
    ],
)
async def test_native_delete_requires_exact_item_and_aware_deletion_time(store, receipt):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"removeItem": receipt}})

    account = SafeAccount(
        "token=private", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    account.id = "seller"
    api = SimpleNamespace(
        account=account,
        lock=asyncio.Lock(),
        call=AsyncMock(return_value={"id": "item", "user": {"id": "seller"}}),
    )
    ident = store.enqueue("delete", "delete", "item", {})
    worker = OutboxWorker(store, Executor(store, api, None))
    try:
        assert await worker.once()
        assert not await worker.once()
        assert len(requests) == 1
        assert json.loads(requests[0].content)["operationName"] == "removeItem"
        assert store.rows("SELECT status,error_code FROM outbox WHERE id=?", (ident,)) == [
            {"status": "UNCERTAIN", "error_code": "DELETE_RESULT_UNKNOWN"}
        ]
    finally:
        account.close()


@pytest.mark.parametrize("deleted", ["2026-10-30T12:00:00Z", "2026-10-30T15:00:00+03:00"])
async def test_native_delete_explicit_receipt_succeeds_without_persisting_body(store, deleted):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": {"removeItem": {"id": "item", "deletedAt": deleted, "description": "private-data"}}
            },
        )

    account = SafeAccount(
        "token=private", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    account.id = "seller"
    api = SimpleNamespace(
        account=account,
        lock=asyncio.Lock(),
        call=AsyncMock(return_value={"id": "item", "user": {"id": "seller"}}),
    )
    store.enqueue("delete", "delete", "item", {})
    try:
        await OutboxWorker(store, Executor(store, api, None)).once()
        assert store.rows("SELECT status,result FROM outbox") == [
            {"status": "SUCCEEDED", "result": '{"removed":true}'}
        ]
        assert len(requests) == 1
        assert "private-data" not in str(list(account.response_diagnostics))
    finally:
        account.close()


@pytest.mark.parametrize("result", [False, None, {}, "true", 1])
async def test_delete_truthy_or_false_stub_is_not_a_success(store, result):
    api = FakeAPI()
    sent = []

    def delete(**kwargs):
        sent.append(kwargs)
        return result

    api.account.remove_item = delete
    store.enqueue("delete", "delete", "item", {})
    worker = OutboxWorker(store, Executor(store, api, None))
    await worker.once()
    assert not await worker.once()
    assert len(sent) == 1
    assert store.rows("SELECT status,error_code FROM outbox") == [
        {"status": "UNCERTAIN", "error_code": "DELETE_RESULT_UNKNOWN"}
    ]
