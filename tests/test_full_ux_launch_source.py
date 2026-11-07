import pytest

from app.services.logical_products import LogicalProducts
from app.services.product_readiness import ProductReadiness
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_unified_delivery_controls import state
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

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


async def test_configured_stock_draft_offers_prepare_then_one_simple_launch(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    logical = LogicalProducts(r.store, r.api)
    logical.stop(7, logical.listing("lot")["id"])
    r.api.item.update(status="DRAFT", raw_price=100)
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_item_priority_statuses":
            return [{"id": "free", "type": "DEFAULT", "price": 0, "period": 7}]
        return await original(method, **kwargs)

    r.api.call = call
    r.store.set_setting("sync:health", {"status": "OK"})
    ProductReadiness(r.store, r.api).remember_item(await r.api.call("get_item", id="lot"))
    await r.ui.click("lp:item:lot")
    assert "launch:prepare:lot" in buttons(r.session.last_panel)
    await r.ui.click("launch:prepare:lot")
    assert "До публикации заказы не обрабатываются" in r.session.last_panel.text
    assert "Приветствие" not in r.session.last_panel.text
    keys = buttons(r.session.last_panel)
    assert sum(key.endswith(":commit") for key in keys) == 1
    assert not any(key.endswith((":modes", ":advanced", ":check", ":edit:data")) for key in keys)
    assert not logical.listing("lot")["enabled"]
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind IN ('publish','premium_publish')")
    assert not r.api.remote.payments and not r.api.messages
    commit = next(key for key in keys if key.endswith(":commit"))
    await r.ui.click(commit)
    assert r.store.rows("SELECT status FROM outbox WHERE kind='publish'") == [{"status": "PENDING"}]
    await r.ui.click(commit)
    assert len(r.store.rows("SELECT id FROM outbox WHERE kind='publish'")) == 1
    assert r.api.item["status"] == "DRAFT" and not r.api.messages
