"""The launch preview explains a conservative inheritance choice before committing it."""

from app.services.logical_products import LogicalProducts
from tests.test_final_modern_gift_creation import shop as shop_fixture

shop = shop_fixture


async def test_launch_explains_both_inheritance_choices_without_implicitly_arming(shop):
    r = shop
    await r.create_and_prepare()
    assert "каждое новое объявление проверяется отдельно" in r.session.last_panel.text
    product = LogicalProducts(r.store, r.api).listing("item")
    assert not product["inherit_enabled"] and not product["enabled"]
    await r.launch_action("inherit")
    assert "перевыставления с тем же кодом" in r.session.last_panel.text
    assert "денежные разрешения" in r.session.last_panel.text
    unchanged = LogicalProducts(r.store, r.api).listing("item")
    assert unchanged == product
    assert not r.api.published and not r.remote.payments
