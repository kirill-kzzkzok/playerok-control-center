"""Old inheritance callbacks cannot change the unified delivery behavior."""

from app.services.logical_products import LogicalProducts
from tests.test_final_modern_gift_creation import shop as shop_fixture
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_old_inheritance_callback_cannot_change_draft_or_arm_product(shop):
    r = shop
    await r.create_and_prepare()
    assert "Запуск продаж" in r.session.last_panel.text
    assert not any(key.endswith(":inherit") for key in buttons(r.session.last_panel))
    draft = r.store.get_draft(r.launch_id, 7)
    product = LogicalProducts(r.store, r.api).listing("item")
    assert not product["inherit_enabled"] and not product["enabled"]
    await r.launch_action("advanced")
    await r.launch_action("inherit")
    assert r.store.get_draft(r.launch_id, 7) == draft
    unchanged = LogicalProducts(r.store, r.api).listing("item")
    assert unchanged == product
    assert not r.api.published and not r.remote.payments
