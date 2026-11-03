"""A mass stop is an authorized, versioned action rather than navigation."""

import pytest

from app.services.logical_products import LogicalProducts
from app.telegram.callback_ack import navigation
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_telegram_transport import production_dispatcher
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize("gift,route", [(True, "gft:stop"), (False, "auto:stop")])
async def test_mass_stop_needs_one_current_confirmation_and_never_repeats(shop, gift, route):
    r = await shop(gift=gift)
    await setup_ui(r, "telegram_gifts" if gift else "unique_stock", "gift: heart" if gift else None)
    before = LogicalProducts(r.store, r.api).listing("lot")
    await r.ui.click(route)
    assert LogicalProducts(r.store, r.api).listing("lot") == before
    confirmation = next(key for key in buttons(r.session.last_panel) if ":stop_confirm:" in key)
    assert "1" in r.session.last_panel.text and "начат" in r.session.last_panel.text
    r.ui.dp = production_dispatcher(r.store, r.api)  # Confirmation is durable across dispatcher restart.
    await r.ui.click(confirmation)
    stopped = LogicalProducts(r.store, r.api).listing("lot")
    assert not stopped["enabled"]
    await r.ui.click(confirmation)
    assert LogicalProducts(r.store, r.api).listing("lot") == stopped
    assert not r.api.messages and not r.api.remote.payments


@pytest.mark.parametrize("route", ["gft:stop", "auto:stop", "promo:stop", "ar:stop"])
def test_mass_stop_is_not_classified_as_safe_navigation(route):
    assert not navigation(route)


async def test_changed_configuration_invalidates_mass_stop_and_other_owner_cannot_confirm(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    logical = LogicalProducts(r.store, r.api)
    before = logical.listing("lot")
    await r.ui.click("gft:stop")
    confirmation = next(key for key in buttons(r.session.last_panel) if ":stop_confirm:" in key)
    await r.ui.click(confirmation, owner=8)
    assert logical.listing("lot") == before
    logical.update_after(7, "lot", before, "Новый текст для будущих покупок")
    updated = logical.listing("lot")
    await r.ui.click(confirmation)
    assert logical.listing("lot") == updated and updated["enabled"]
    assert "Настройки уже изменились" in r.session.last_panel.text
    assert not r.api.messages and not r.api.remote.payments
