import pytest

from app.services.logical_products import LogicalProducts
from tests.test_final_modern_gift_creation import shop as shop_fixture

shop = shop_fixture


@pytest.mark.parametrize("entry", ["original", "publish:item"])
async def test_new_creation_launch_keeps_confirmed_relist_inheritance_default(shop, entry):
    r = shop
    await r.create_and_prepare(prepare_launch=False)
    await r.ui.click("launch:from:" + r.original_draft if entry == "original" else entry)
    view = (await r.state.get_data())["launch"]
    draft = r.store.get_draft(view["id"], 7)
    # Historical launch payloads cannot opt a new publication out of inheritance.
    r.store.update_draft(view["id"], 7, draft["revision"], {**draft["payload"], "inherit": False})
    assert not r.api.published and not r.remote.payments
    await r.ui.click("launch:open:" + view["id"])
    await r.launch_action("check")
    await r.launch_action("commit")
    assert await r.worker.once()
    assert len(r.api.published) == 1 and not r.remote.payments
    assert LogicalProducts(r.store, r.api).listing("item")["inherit_enabled"] == 1
