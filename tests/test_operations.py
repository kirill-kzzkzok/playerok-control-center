import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.database.store import Conflict, Store
from app.playerok.adapter import APIError
from app.services.products import Products
from app.workers.outbox import Executor, OutboxWorker


class FakeAccount:
    id = "seller"
    writes_enabled = True

    def __init__(self):
        self.calls = []

    @contextmanager
    def permit(self, operation):
        self.calls.append(operation)
        yield

    def create_item(self, **kwargs):
        return SimpleNamespace(id="remote1", status="DRAFT")

    def publish_item(self, **kwargs):
        return SimpleNamespace(id=kwargs["item_id"], status="PENDING_APPROVAL")

    def update_item(self, **kwargs):
        return SimpleNamespace(id=kwargs["id"])

    def remove_item(self, **kwargs):
        return True

    def send_message(self, **kwargs):
        return SimpleNamespace(id="message1")


class FakeAPI:
    def __init__(self):
        self.account = FakeAccount()
        self.lock = asyncio.Lock()
        self.quote = {"id": "free", "type": "DEFAULT", "price": 0}
        self.item_owner = "seller"

    async def call(self, method, **kwargs):
        if method == "get_game_category":
            return {"options": []}
        if method == "get_item":
            return {"id": kwargs["id"], "user": {"id": self.item_owner}, "status": "DRAFT", "price": 100}
        if method == "get_item_priority_statuses":
            return [self.quote]
        if method == "get_chat":
            assert "chat_id" in kwargs
            return {"id": kwargs["chat_id"]}
        raise AssertionError(method)

    async def pages(self, method, collection, **kwargs):
        if method == "get_game_category_obtaining_types":
            yield [{"id": "o"}]
        else:
            assert method == "get_game_category_data_fields"
            yield []


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "operations.sqlite")
    api = FakeAPI()
    products = Products(store, api)
    yield store, api, products, OutboxWorker(store, Executor(store, api, products))
    store.close()


async def test_create_double_click_and_publish(setup):
    store, api, products, worker = setup
    ident = store.draft(
        1,
        {
            "category_id": "c",
            "obtaining_type_id": "o",
            "name": "Test",
            "description": "test",
            "price": 100,
            "fields": {},
            "photos": [],
        },
    )
    results = await asyncio.gather(
        products.queue_create(ident, 1, 0), products.queue_create(ident, 1, 0), return_exceptions=True
    )
    assert sum(isinstance(r, Conflict) for r in results) == 1
    await worker.once()
    assert api.account.calls == ["createItem"]
    assert not await worker.once()
    assert store.rows("SELECT remote_id FROM bindings")[0]["remote_id"] == "remote1"
    store.enqueue("publish:remote1", "publish", "remote1", {"priority_id": "free", "price": 100})
    await worker.once()
    assert api.account.calls == ["createItem", "publishItem"]


async def test_stale_paid_quote_blocks_before_mutation(setup):
    store, api, _, worker = setup
    api.quote["price"] = 1
    store.enqueue("publish:remote1", "publish", "remote1", {"priority_id": "free", "price": 100})
    await worker.once()
    assert not api.account.calls
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "PERMANENT_FAILED"


@pytest.mark.parametrize("kind,payload", [("edit", {"price": 100}), ("delete", {})])
async def test_foreign_item_write_forbidden(setup, kind, payload):
    store, api, _, worker = setup
    api.item_owner = "another seller"
    store.enqueue("op", kind, "remote1", payload)
    await worker.once()
    assert not api.account.calls


async def test_edit_delete_reply_dispatch_real_method_signatures(setup):
    store, api, _, worker = setup
    for kind, payload in [("edit", {"price": 200}), ("delete", {}), ("reply", {"text": "hello"})]:
        store.enqueue(kind, kind, "remote1", payload)
        await worker.once()
    assert api.account.calls == ["updateItem", "removeItem", "createChatMessage"]
    assert all(r["status"] == "SUCCEEDED" for r in store.rows("SELECT status FROM outbox"))


async def test_response_lost_after_remote_create_not_repeated(setup):
    store, api, products, worker = setup

    def create(**kwargs):
        raise APIError("NETWORK_ERROR", uncertain=True)

    api.account.create_item = create
    ident = store.draft(
        1,
        {
            "category_id": "c",
            "obtaining_type_id": "o",
            "name": "Test",
            "description": "test",
            "price": 100,
            "fields": {},
            "photos": [],
        },
    )
    await products.queue_create(ident, 1, 0)
    await worker.once()
    assert not await worker.once()
    assert api.account.calls == ["createItem"]
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
