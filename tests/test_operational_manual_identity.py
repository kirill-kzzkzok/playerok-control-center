"""Manual creation gets identity without granting delivery authority."""

import pytest

from app.services.logical_products import LogicalProducts
from tests.test_final_modern_gift_creation import shop as shop_fixture

shop = shop_fixture


@pytest.mark.parametrize("name", ["x" * 66, "🧸" * 33])
async def test_manual_wizard_reserves_one_code_with_no_automation(shop, name):
    r = shop
    await r.create_and_prepare(stop_before_delivery=True, description="Description unchanged")
    await r.wizard_action("choose:0")
    await r.wizard_action("confirm_done")
    await r.wizard_action("edit:name")
    await r.ui.message(name)
    submit = "w:" + (await r.state.get_data())["view_token"] + ":submit"
    await r.wizard_action("submit")
    await r.worker.once()
    product = LogicalProducts(r.store, r.api).listing("item")
    assert product and product["state"] == "VERIFIED"
    assert product["handler"] == "manual" and not product["enabled"] and not product["armed_at"]
    assert r.api.item["name"] == name + " [" + product["code"] + "]"
    assert len(r.api.item["name"].encode("utf-16-le")) // 2 == 72
    assert r.api.item["description"] == "Description unchanged"
    assert len(r.store.rows("SELECT * FROM logical_code_reservations")) == 1
    assert not r.store.rows("SELECT * FROM delivery_policies")
    assert not r.store.rows("SELECT * FROM gift_listings")
    assert not r.store.rows("SELECT * FROM reservations")
    assert not r.api.published and not r.api.sent and not r.remote.payments
    # A repeated old submit cannot create another item or reserve another code.
    await r.ui.click(submit)
    assert not await r.worker.once()
    assert len(r.api.created) == 1
    assert len(r.store.rows("SELECT * FROM logical_products")) == 1
