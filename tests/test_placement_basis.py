import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.database.store import Store
from app.services.products import Products, ValidationError, priority_price_basis
from app.workers.outbox import Executor, OutboxWorker


def test_promotion_uses_real_base_not_discounted_sale_price():
    assert priority_price_basis({"price": 800, "raw_price": 1000}) == 1000
    with pytest.raises(ValidationError):
        priority_price_basis({"price": 800})


@pytest.mark.parametrize("changed", [False, True])
async def test_free_publication_quote_uses_base_and_rechecks_both_prices(tmp_path, changed):
    store = Store(tmp_path / "placement.sqlite")
    published, requested = [], []

    class Account:
        id = "seller"
        writes_enabled = True

        @contextmanager
        def permit(self, operation):
            assert operation == "publishItem"
            yield

        def publish_item(self, **kwargs):
            published.append(kwargs)
            return SimpleNamespace(id=kwargs["item_id"], status="PENDING_APPROVAL")

    class API:
        account = Account()
        lock = asyncio.Lock()

        async def call(self, method, **kwargs):
            if method == "get_item":
                return {
                    "id": "item",
                    "user": {"id": "seller"},
                    "status": "DRAFT",
                    "price": 800,
                    "raw_price": 1100 if changed else 1000,
                }
            assert method == "get_item_priority_statuses"
            requested.append(kwargs["item_price"])
            return [{"id": "free", "type": "DEFAULT", "price": 0}]

    api = API()
    try:
        store.enqueue("publish", "publish", "item", {"priority_id": "free", "price": 800, "raw_price": 1000})
        await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
        assert requested == [1100 if changed else 1000]
        assert len(published) == (0 if changed else 1)
        assert store.rows("SELECT status FROM outbox")[0]["status"] == (
            "PERMANENT_FAILED" if changed else "SUCCEEDED"
        )
    finally:
        store.close()
