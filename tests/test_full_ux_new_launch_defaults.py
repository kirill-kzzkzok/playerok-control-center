import pytest

from tests.test_final_modern_gift_creation import shop as shop_fixture

shop = shop_fixture


@pytest.mark.parametrize("entry", ["original", "publish:item"])
async def test_new_creation_launch_keeps_confirmed_relist_inheritance_default(shop, entry):
    r = shop
    await r.create_and_prepare(prepare_launch=False)
    await r.ui.click("launch:from:" + r.original_draft if entry == "original" else entry)
    view = (await r.state.get_data())["launch"]
    assert r.store.get_draft(view["id"], 7)["payload"]["inherit"] is True
    assert not r.api.published and not r.remote.payments
