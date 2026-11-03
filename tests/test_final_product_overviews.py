"""Overview buttons follow permanent identity across real logical relisting."""

from copy import deepcopy

import pytest

from app.services.orders import Orders
from app.workers.logical_products import LogicalProductWorker
from tests.test_playerok_delivery import configure
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize("handler,value", [("unique_stock", None), ("telegram_gifts", "gift: heart")])
async def test_overviews_show_one_current_logical_product_and_keep_old_history(shop, handler, value):
    r = await shop(gift=handler == "telegram_gifts")
    await setup_ui(r, handler, value)
    Orders(r.store).ingest(r.api.order("historical-sale", status="CONFIRMED"))
    r.api.item["status"] = "SOLD"
    r.api.items["next-lot"] = {**deepcopy(r.api.item), "id": "next-lot", "status": "APPROVED"}
    await LogicalProductWorker(r.store, r.api).once()
    for route in ("automation", "aq:list:policies:0"):
        await r.ui.click(route)
        links = [key for key in buttons(r.session.last_panel) if key.startswith(("item:", "auto:item:"))]
        assert links == ["item:next-lot"]
    if handler == "telegram_gifts":
        await r.ui.click("gft:bindings")
        assert [key for key in buttons(r.session.last_panel) if key.startswith("gft:item:")] == [
            "gft:item:next-lot"
        ]
    await r.ui.click("iv:history:next-lot")
    assert "order:historical-sale" in buttons(r.session.last_panel)
    assert not r.api.messages and not r.api.remote.payments


async def test_platform_product_remains_in_overview_but_not_the_gift_list(shop):
    r = await shop()
    await configure(r)
    await r.ui.click("automation")
    assert "item:lot" in buttons(r.session.last_panel)
    await r.ui.click("gft:bindings")
    assert not any(key.startswith("gft:item:") for key in buttons(r.session.last_panel))
    assert not r.api.messages and not r.api.remote.payments
