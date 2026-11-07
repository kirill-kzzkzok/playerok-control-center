"""Old inheritance callbacks stage choices without arming the saved product."""

from app.services.logical_products import LogicalProducts
from tests.test_final_modern_gift_creation import shop as shop_fixture
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_old_inheritance_callbacks_stage_both_choices_without_implicitly_arming(shop):
    r = shop
    await r.create_and_prepare()
    assert "Запуск продаж" in r.session.last_panel.text
    assert not any(key.endswith(":inherit") for key in buttons(r.session.last_panel))
    assert r.store.get_draft(r.launch_id, 7)["payload"]["inherit"] is True
    product = LogicalProducts(r.store, r.api).listing("item")
    assert not product["inherit_enabled"] and not product["enabled"]
    await r.launch_action("advanced")
    await r.launch_action("inherit")
    assert r.store.get_draft(r.launch_id, 7)["payload"]["inherit"] is False
    await r.launch_action("advanced")
    await r.launch_action("inherit")
    assert r.store.get_draft(r.launch_id, 7)["payload"]["inherit"] is True
    unchanged = LogicalProducts(r.store, r.api).listing("item")
    assert unchanged == product
    assert not r.api.published and not r.remote.payments
