import pytest

from app.database.store import Store
from app.services.logical_products import LogicalProducts, marker
from app.services.products import Products
from app.workers.outbox import Executor, OutboxWorker
from tests.test_logical_products import ReadAPI


def test_runtime_marker_uses_only_title_suffix():
    assert marker("Product [aB6]") == "AB6"
    assert marker("Description\n#2163148") is None


@pytest.mark.parametrize("lost", [False, True])
async def test_bind_title_and_description_readback_with_no_duplicate_after_unknown(tmp_path, lost):
    store = Store(tmp_path / "runtime.sqlite")
    store.claim_first_owner(7)
    store.set_setting("playerok:account_id", "seller")
    api = ReadAPI()
    service = LogicalProducts(store, api)
    original = api.item["description"]
    prepared = await service.prepare(7, "lot", "fixed_text", {"text": "private data"})
    result = service.commit(7, prepared)
    api.lost_ack = lost
    worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
    await worker.once()
    product = service.product(result["product_id"])
    assert marker(api.item["name"]) == product["code"]
    assert api.item["description"] == original
    assert len(product["code"]) == 3
    assert len(api.mutations) == 1
    if lost:
        assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
        assert not await worker.once()
        await service.reconcile_binding(7, product["id"])
    assert store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
    service.verify_listing(service.product(product["id"]), api.item)
    assert service.product(product["id"])["enabled"] == 0
    assert service.commit(7, prepared) == result
    assert len(api.mutations) == 1
    store.close()


from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_unified_delivery_controls import state, stop
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


async def test_dispatcher_configure_restart_and_rename_keeps_title_code(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private unit")
    product = r.store.rows("SELECT id,code FROM logical_products")[0]
    assert marker(r.api.item["name"]) == product["code"]
    assert "#" not in r.api.item["description"]
    await stop(r)
    await r.ui.click("editname:lot")
    await r.ui.message("Новое название")
    pending = (await state(r).get_data())["pending"]["payload"]
    assert pending["name"] == "Новое название [" + product["code"] + "]"
    await r.ui.click("commit:" + (await state(r).get_data())["confirm_token"])
    await r.worker.once()
    assert r.api.item["name"] == pending["name"]
    assert r.store.rows("SELECT id,code FROM logical_products") == [product]
    logical = LogicalProducts(r.store, r.api)
    logical.verify_listing(logical.listing("lot"), r.api.item)
    assert not r.api.messages and not r.api.remote.payments
