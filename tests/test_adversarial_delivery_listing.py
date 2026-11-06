from copy import deepcopy

import pytest

from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_logical_faults import reopen
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


@pytest.mark.parametrize("mode", ["unique_stock", "fixed_text"])
@pytest.mark.parametrize("field", ["user", "category", "status", "description"])
async def test_incomplete_fresh_listing_never_permanently_blocks_paid_sale(shop, mode, field):
    r = await shop()
    await setup_ui(r, mode, "delivery" if mode == "fixed_text" else None)
    Orders(r.store).ingest(r.api.order("listing-read"))
    original = r.api.call
    incomplete = True

    async def read(method, **kwargs):
        item = await original(method, **kwargs)
        if incomplete and method == "get_item":
            item = deepcopy(item)
            item.pop(field)
        return item

    r.api.call = read
    await EnrollmentWorker(r.store, r.api).once()
    assert r.store.rows("SELECT status FROM auto_enrollment") == [{"status": "PENDING"}]
    assert not r.api.messages and not r.store.rows("SELECT * FROM reservations")
    incomplete = False
    reopen(r)
    r.store.execute("UPDATE auto_enrollment SET due_at=0")
    for _ in range(8):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    expected = "delivery" if mode == "fixed_text" else "isolated-unit-one"
    assert sum(m["text"] == expected for m in r.api.messages) == 1
