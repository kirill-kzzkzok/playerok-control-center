import asyncio
import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, Store
from app.playerok.adapter import SafeAccount
from app.services.products import Products, ValidationError
from app.telegram.storage import SQLiteStorage
from app.telegram.wizard import Wizard
from app.workers.outbox import Executor, OutboxWorker
from tests.transport_helpers import mutate


class FieldsAccount:
    id = "seller"
    writes_enabled = True

    def __init__(self):
        self.calls = []

    @contextmanager
    def permit(self, operation):
        assert operation == "updateItem"
        yield

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=kwargs["id"])


class FieldsAPI:
    def __init__(self):
        self.account = FieldsAccount()
        self.lock = asyncio.Lock()
        self.item = {
            "id": "item",
            "user": {"id": "seller"},
            "is_editable": True,
            "category": {"id": "category"},
            "obtaining_type": None,
            "game": {"id": "game"},
            "name": "Real item fixture",
            "description": "Existing description",
            "price": 500,
            "attributes": {"region": "eu", "opaque": "preserved"},
            "comment": "private old data",
            "data_fields": [],
        }

    async def call(self, method, **kwargs):
        if method == "get_item":
            return deepcopy(self.item)
        if method == "get_game_category":
            return {
                "id": "category",
                "options": [
                    {"field": "region", "label": value, "group": "Region", "type": "SELECTOR", "value": value}
                    for value in ["eu", "us"]
                ],
            }
        raise AssertionError(method)

    async def pages(self, method, collection, **kwargs):
        yield []


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "edit.sqlite")
    api = FieldsAPI()
    products = Products(store, api)
    wizard = Wizard(store, api, products)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=1, user_id=1))
    yield store, api, products, wizard, state
    store.close()


async def click(wizard, state, action):
    nonce = (await state.get_data())["view_token"]
    return await wizard.callback(1, state, f"w:{nonce}:{action}")


async def test_existing_fields_edit_is_a_separate_draft_never_create(setup):
    store, api, products, wizard, state = setup
    await wizard.start_edit(1, state, "item")
    ident = (await state.get_data())["draft_id"]
    with pytest.raises(ValidationError, match="нельзя"):
        await products.queue_create(ident, 1, 1)
    await click(wizard, state, "choose:1")
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), text="private replacement data")
    text, _ = await wizard.message(message, state)
    assert "private replacement data" not in text and "private old data" not in text
    assert "Редактирование" in text
    await click(wizard, state, "submit")
    action = store.rows("SELECT * FROM outbox")[0]
    assert action["kind"] == "edit_fields" and action["target"] == "item"
    await OutboxWorker(store, Executor(store, api, products)).once()
    assert len(api.account.calls) == 1
    changes = api.account.calls[0]
    assert changes["comment"] == "private replacement data"
    assert {o.field: o.value for o in changes["options"]} == {"region": "us", "opaque": "preserved"}
    assert "price" not in changes and "description" not in changes and "name" not in changes
    assert store.get_draft(ident, 1)["state"] == "UPDATED"


async def test_keep_existing_private_values_and_restart(setup):
    store, _, _, wizard, state = setup
    await wizard.start_edit(1, state, "item")
    await click(wizard, state, "field_keep")
    state = FSMContext(SQLiteStorage(store), state.key)
    text, _ = await click(wizard, state, "field_keep")
    assert "private old data" not in text
    ident = (await state.get_data())["draft_id"]
    assert store.get_draft(ident, 1)["payload"]["fields"]["__comment"] == "private old data"
    assert store.get_draft(ident, 1)["payload"]["step"] == "preview"


async def test_remote_change_blocks_before_queue_and_again_before_execution(setup):
    store, api, products, wizard, state = setup
    await wizard.start_edit(1, state, "item")
    await click(wizard, state, "field_keep")
    await click(wizard, state, "field_keep")
    draft = store.get_draft((await state.get_data())["draft_id"], 1)
    api.item["attributes"]["region"] = "us"
    with pytest.raises(Conflict):
        await products.queue_fields_edit(draft["id"], 1, draft["revision"])
    api.item["attributes"]["region"] = "eu"
    await products.queue_fields_edit(draft["id"], 1, draft["revision"])
    api.item["comment"] = "changed elsewhere"
    await OutboxWorker(store, Executor(store, api, products)).once()
    assert not api.account.calls
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "PERMANENT_FAILED"


async def test_foreign_or_noneditable_item_cannot_open_editor(setup):
    store, api, products, _, _ = setup
    api.item["user"]["id"] = "other"
    with pytest.raises(ValidationError):
        await products.edit_fields_draft(1, "item")
    api.item["user"]["id"] = "seller"
    api.item["is_editable"] = False
    with pytest.raises(ValidationError):
        await products.edit_fields_draft(1, "item")
    assert not store.rows("SELECT * FROM drafts")


async def test_update_transport_serializes_comment_as_private_input():
    def respond(request):
        body = json.loads(request.content)
        assert body["variables"]["input"] == {"id": "item", "comment": "private new value"}
        return httpx.Response(200, json={"data": {"updateItem": {"id": "item", "__typename": "MyItem"}}})

    account = SafeAccount(
        "token=private", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    try:
        await mutate(account, "updateItem", "update_item", id="item", comment="private new value")
    finally:
        account.close()
