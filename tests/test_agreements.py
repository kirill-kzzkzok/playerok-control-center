import asyncio
import json
from contextlib import contextmanager
from copy import deepcopy
from html import unescape

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, Store
from app.playerok.adapter import APIError, SafeAccount
from app.services.agreements import Agreements
from app.services.products import Products, ValidationError
from app.telegram.formatting import html_pages
from app.telegram.storage import SQLiteStorage
from app.telegram.wizard import Wizard
from app.workers.outbox import Executor, OutboxWorker
from tests.transport_helpers import mutate


def agreement(ident="a", description="Условия продажи <товара>"):
    return {
        "id": ident,
        "description": description,
        "game_category_id": "category",
        "game_category_obtaining_type_id": None,
    }


class AgreementAccount:
    id = "seller"
    writes_enabled = False

    def __init__(self, api):
        self.api = api
        self.accepted = []
        self.fail_id = None

    @contextmanager
    def permit(self, operation):
        assert operation == "acceptGameCategoryAgreement"
        if not self.writes_enabled:
            raise APIError("WRITES_DISABLED")
        yield

    def accept_game_category_agreement(self, agreement_id):
        self.accepted.append(agreement_id)
        if agreement_id == self.fail_id:
            raise APIError("NETWORK_ERROR", uncertain=True)
        self.api.pending = [value for value in self.api.pending if value["id"] != agreement_id]
        return {"id": agreement_id}


class AgreementAPI:
    def __init__(self):
        self.pending = [agreement()]
        self.category = {
            "id": "category",
            "options": [],
            "agreements": [agreement()],
            "instruction_for_seller": "Проверенная инструкция. " * 180,
        }
        self.account = AgreementAccount(self)
        self.lock = asyncio.Lock()

    async def call(self, method, **kwargs):
        assert method == "get_game_category"
        return deepcopy(self.category)

    async def pages(self, method, collection, **kwargs):
        if method == "get_game_category_agreements":
            yield deepcopy(self.pending)
        elif method in {"get_game_category_data_fields", "get_game_category_obtaining_types"}:
            yield []
        else:
            raise AssertionError(method)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "terms.sqlite")
    api = AgreementAPI()
    products = Products(store, api)
    wizard = Wizard(store, api, products)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=1, user_id=1))
    yield store, api, products, wizard, state
    store.close()


async def initialize(store, wizard, state):
    ident = store.draft(1, {"step": "obtaining", "category_id": "category", "fields": {}, "photos": []})
    await state.set_state("wizard")
    await state.set_data({"draft_id": ident, "revision": 0})
    await wizard.screen(1, state)
    return ident


async def click(wizard, state, action):
    nonce = (await state.get_data())["view_token"]
    return await wizard.callback(1, state, f"w:{nonce}:{action}")


async def read_to_end(store, wizard, state, ident):
    while True:
        payload = store.get_draft(ident, 1)["payload"]
        if payload["conditions_page"] == len(wizard.condition_pages(payload)) - 1:
            return
        await click(wizard, state, "conditions_next")


async def test_conditions_full_review_explicit_accept_then_fresh_confirmation(setup):
    store, api, products, wizard, state = setup
    ident = await initialize(store, wizard, state)
    assert store.get_draft(ident, 1)["payload"]["step"] == "conditions"
    with pytest.raises(ValidationError, match="все страницы"):
        await click(wizard, state, "conditions_done")
    await read_to_end(store, wizard, state, ident)
    with pytest.raises(ValidationError, match="отключены"):
        await click(wizard, state, "conditions_done")
    assert not store.rows("SELECT * FROM outbox")
    api.account.writes_enabled = True
    await click(wizard, state, "conditions_done")
    await click(wizard, state, "conditions_done")
    assert len(store.rows("SELECT * FROM outbox")) == 1
    assert api.account.accepted == []  # callbacks only enqueue
    await OutboxWorker(store, Executor(store, api, products)).once()
    assert api.account.accepted == ["a"]
    await click(wizard, state, "conditions_refresh")
    await read_to_end(store, wizard, state, ident)
    await click(wizard, state, "conditions_done")
    payload = store.get_draft(ident, 1)["payload"]
    assert payload["step"] == "fields"
    assert payload["conditions_ack"] == payload["conditions"]["hash"]


async def test_creation_blocked_when_pending_or_instructions_change(setup):
    store, api, products, _, _ = setup
    details = await products.schema_details("category", None)
    payload = {
        "category_id": "category",
        "obtaining_type_id": None,
        "obtaining_not_required": True,
        "name": "Test product",
        "description": "Test description",
        "price": 100,
        "fields": {"__comment": "Private data"},
        "conditions_ack": details["conditions"]["hash"],
    }
    ident = store.draft(1, payload)
    with pytest.raises(ValidationError, match="непринятые"):
        await products.queue_create(ident, 1, 0)
    api.pending = []
    api.category["instruction_for_seller"] += " Новое обязательное условие."
    with pytest.raises(ValidationError, match="изменились"):
        await products.queue_create(ident, 1, 0)
    assert not store.rows("SELECT * FROM outbox")
    assert store.get_draft(ident, 1)["state"] == "DRAFT"


async def test_stale_acceptance_never_accepts_changed_terms(setup):
    store, api, products, wizard, state = setup
    ident = await initialize(store, wizard, state)
    api.account.writes_enabled = True
    await read_to_end(store, wizard, state, ident)
    await click(wizard, state, "conditions_done")
    api.pending[0]["description"] = "Изменённое соглашение"
    await OutboxWorker(store, Executor(store, api, products)).once()
    assert not api.account.accepted
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "PERMANENT_FAILED"


async def test_partially_accepted_terms_timeout_is_uncertain_without_replay(setup):
    store, api, products, _, _ = setup
    api.pending = [agreement("a"), agreement("b")]
    api.account.writes_enabled = True
    api.account.fail_id = "b"
    store.enqueue(
        "accept",
        "agreements",
        "category",
        {"account_id": "seller", "obtaining_type_id": None, "agreements": deepcopy(api.pending)},
    )
    worker = OutboxWorker(store, Executor(store, api, products))
    await worker.once()
    assert not await worker.once()
    assert api.account.accepted == ["a", "b"]
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
    with pytest.raises(Conflict):
        store.enqueue("accept-retry", "agreements", "category", {"agreements": deepcopy(api.pending)})


async def test_already_accepted_terms_reconciled_without_sending(setup):
    store, api, products, _, _ = setup
    api.pending = []
    store.enqueue(
        "accept",
        "agreements",
        "category",
        {"account_id": "seller", "obtaining_type_id": None, "agreements": [agreement()]},
    )
    await OutboxWorker(store, Executor(store, api, products)).once()
    assert not api.account.accepted
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"


def test_paginated_terms_do_not_drop_or_split_html_entities():
    source = "<условия> & 'проверка' 😀" * 500
    pages = html_pages(source)
    assert len(pages) > 1 and all(len(page) <= 2800 for page in pages)
    assert "".join(unescape(page) for page in pages) == source


async def test_acceptance_transport_uses_confirmed_input_and_current_account():
    received = []

    def respond(request):
        data = json.loads(request.content)
        received.append(data)
        assert data["operationName"] == "acceptGameCategoryAgreement"
        assert data["variables"] == {"input": {"agreementId": "terms", "userId": "current-account"}}
        return httpx.Response(200, json={"data": {"acceptGameCategoryAgreement": {"id": "terms"}}})

    account = SafeAccount(
        "token=private", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    account.id, account._is_initiated = "current-account", True
    try:
        await mutate(
            account, "acceptGameCategoryAgreement", "accept_game_category_agreement", agreement_id="terms"
        )
        assert len(received) == 1
    finally:
        account.close()


async def test_obtaining_specific_terms_filtered_and_wrong_category_rejected(setup):
    _, api, _, _, _ = setup
    other = agreement("other")
    other["game_category_obtaining_type_id"] = "another-method"
    api.pending.append(other)
    assert len(await Agreements(api).pending("category", None)) == 1
    api.pending[0]["game_category_id"] = "another-category"
    with pytest.raises(ValidationError, match="категории"):
        await Agreements(api).pending("category", None)
