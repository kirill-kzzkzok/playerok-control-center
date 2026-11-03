"""Acknowledged partial item replies may use exact postconditions, never retry."""

import pytest

from app.database.store import Store
from app.services.discounts import discount_plan
from app.services.products import Products
from app.workers.outbox import Executor, OutboxWorker
from tests.test_discounts import DiscountAPI
from tests.test_item_fields import click
from tests.test_item_fields import setup as fields_fixture
from tests.test_operations import FakeAPI, queue_manual_publication

fields = fields_fixture


@pytest.mark.parametrize("kind", ["create", "publish", "discount"])
async def test_acknowledged_partial_item_receipt_confirms_exact_server_state_once(tmp_path, kind):
    store = Store(tmp_path / "receipt.sqlite")
    api = DiscountAPI() if kind == "discount" else FakeAPI()
    products = Products(store, api)
    if kind == "create":
        ident = store.draft(
            1,
            {
                "category_id": "c",
                "obtaining_type_id": "o",
                "name": "Test product",
                "description": "Test description",
                "price": 100,
                "fields": {"__comment": "private data"},
                "photos": [],
            },
        )
        await products.queue_create(ident, 1, 0)
    elif kind == "publish":
        await queue_manual_publication(store, api, "item")
    else:
        store.enqueue("discount", "discount", "item", discount_plan(api.item, 800))
    method = {"create": "create_item", "publish": "publish_item", "discount": "update_item"}[kind]
    original = getattr(api.account, method)
    writes = []

    def partial(**kwargs):
        writes.append(kwargs)
        result = original(**kwargs)
        return {"id": result.id}

    api.account.__dict__[method] = partial
    try:
        worker = OutboxWorker(store, Executor(store, api, products))
        await worker.once()
        assert store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
        assert len(writes) == 1 and not await worker.once()
        if kind == "create":
            assert len(store.rows("SELECT * FROM bindings")) == 1
    finally:
        store.close()


async def test_fields_partial_ack_without_item_id_uses_existing_exact_readback(fields):
    store, api, products, wizard, state = fields
    await wizard.start_edit(1, state, "item")
    await click(wizard, state, "field_keep")
    await click(wizard, state, "field_keep")
    draft_id = (await state.get_data())["draft_id"]
    await click(wizard, state, "submit")
    original = api.account.update_item

    def partial(**kwargs):
        original(**kwargs)
        return {}

    api.account.update_item = partial
    worker = OutboxWorker(store, Executor(store, api, products))
    await worker.once()
    assert store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
    assert store.get_draft(draft_id, 1)["state"] == "UPDATED"
    assert not await worker.once() and len(api.account.calls) == 1
