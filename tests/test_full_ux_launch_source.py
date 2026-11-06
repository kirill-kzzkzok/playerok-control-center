import pytest

from app.services.logical_products import LogicalProducts
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_unified_delivery_controls import state
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


@pytest.mark.parametrize(
    "handler,content", [("fixed_text", "delivery text"), ("telegram_gifts", "gift: heart")]
)
async def test_launch_uses_current_saved_delivery_settings_without_reset(shop, handler, content):
    r = await shop(gift=handler == "telegram_gifts")
    await setup_ui(r, handler, content)
    logical = LogicalProducts(r.store, r.api)
    fields = [("after", "Saved thanks"), ("completion", False)]
    if handler == "fixed_text":
        fields.append(("greeting", "Saved greeting"))
    for field, value in fields:
        logical.update_after(7, "lot", logical.listing("lot"), value, field=field)
    product = logical.listing("lot")
    r.api.item["status"] = "DRAFT"
    await r.ui.click("publish:lot")
    context = (await state(r).get_data())["launch"]
    value = r.store.get_draft(context["id"], 7)["payload"]
    assert value["after"] == "Saved thanks"
    assert value["greeting"] == ("Saved greeting" if handler == "fixed_text" else "")
    assert value["auto_complete"] is False
    assert value["inherit"] is bool(product["inherit_enabled"])
    assert not r.api.remote.payments
