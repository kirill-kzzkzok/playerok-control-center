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
@pytest.mark.parametrize("reserved", [False, True])
@pytest.mark.parametrize("field", ["id", "status", "direction", "item_id"])
async def test_missing_core_deal_fields_wait_then_resume_original_admission(shop, mode, reserved, field):
    r = await shop()
    await setup_ui(r, mode, "delivery" if mode == "fixed_text" else None)
    Orders(r.store).ingest(r.api.order("partial-core"))
    if reserved:
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
    original = r.api.call
    incomplete = True

    async def read(method, **kwargs):
        result = await original(method, **kwargs)
        if incomplete and method in {"get_deal", "get_delivery_deal"}:
            result = deepcopy(result)
            if field == "item_id":
                result["item"].pop("id")
            else:
                result.pop(field)
        return result

    r.api.call = read
    if reserved:
        await r.worker.once()
        assert r.store.rows("SELECT status FROM outbox WHERE kind IN ('delivery','delivery_text')") == [
            {"status": "RETRYABLE_FAILED"}
        ]
    else:
        await EnrollmentWorker(r.store, r.api).once()
        assert r.store.rows("SELECT status FROM auto_enrollment") == [{"status": "PENDING"}]
        assert not r.store.rows("SELECT * FROM reservations")
    assert not r.api.messages
    incomplete = False
    reopen(r)
    r.store.execute("UPDATE auto_enrollment SET due_at=0")
    r.store.execute("UPDATE outbox SET retry_at=0")
    for _ in range(8):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    payload = "delivery" if mode == "fixed_text" else "isolated-unit-one"
    assert sum(m["text"] == payload for m in r.api.messages) == 1
    assert sum(m["text"] == "Спасибо за покупку!" for m in r.api.messages) == 1
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "SUCCEEDED"}]
