import asyncio
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.database.store import Store
from app.services.discounts import discount_plan
from app.services.products import Products, ValidationError
from app.workers.outbox import Executor, OutboxWorker


def item(**changes):
    return {
        "id": "item",
        "user": {"id": "seller"},
        "is_editable": True,
        "price": 1000,
        "raw_price": 1000,
        **changes,
    }


@pytest.mark.parametrize("sale", [951, 999, 1001, 89, "NaN"])
def test_real_discount_bounds_from_official_edit_form(sale):
    with pytest.raises(ValidationError):
        discount_plan(item(), sale)


def test_discount_sets_sale_price_without_fabricating_old_price():
    plan = discount_plan(item(), 800)
    assert plan == {
        "price": 800,
        "expected_price": 1000,
        "expected_raw_price": 1000,
        "discount_percent": "20.00",
        "restore": False,
    }
    assert "raw_price" not in plan
    assert discount_plan(item(price=800), 1000)["restore"]


def test_floor_maximum_and_no_discount_when_minimum_leaves_no_room():
    assert discount_plan(item(price=109, raw_price=109), 103)["price"] == 103
    with pytest.raises(ValidationError):
        discount_plan(item(price=109, raw_price=109), 104)
    with pytest.raises(ValidationError):
        discount_plan(item(price=95, raw_price=95), 90)


@pytest.mark.parametrize("changes", [{"raw_price": None}, {"price": None}])
def test_missing_server_prices_never_invented(changes):
    with pytest.raises(ValidationError):
        discount_plan(item(**changes), 800)


class DiscountAccount:
    id = "seller"
    writes_enabled = True

    def __init__(self, api):
        self.api, self.calls = api, []

    @contextmanager
    def permit(self, operation):
        assert operation == "updateItem"
        yield

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        self.api.item["price"] = kwargs["price"]
        return SimpleNamespace(**deepcopy(self.api.item))


class DiscountAPI:
    def __init__(self):
        self.item = item()
        self.account = DiscountAccount(self)
        self.lock = asyncio.Lock()

    async def call(self, method, **kwargs):
        assert method == "get_item"
        return deepcopy(self.item)


async def test_discount_rechecks_snapshot_and_requires_confirmed_server_prices(tmp_path):
    store = Store(tmp_path / "discounts.sqlite")
    api = DiscountAPI()
    products = Products(store, api)
    worker = OutboxWorker(store, Executor(store, api, products))
    try:
        plan = discount_plan(api.item, 800)
        store.enqueue("discount", "discount", "item", plan)
        await worker.once()
        assert api.account.calls == [{"id": "item", "price": 800}]
        assert store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
        restore = discount_plan(api.item, 1000)
        store.enqueue("restore", "discount", "item", restore)
        api.item["raw_price"] = 1100  # external change after owner's preview
        await worker.once()
        assert len(api.account.calls) == 1
        assert (
            store.rows("SELECT status FROM outbox WHERE operation_key='restore'")[0]["status"]
            == "PERMANENT_FAILED"
        )
    finally:
        store.close()
