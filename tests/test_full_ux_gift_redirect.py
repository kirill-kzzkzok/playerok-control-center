import pytest

from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_unified_delivery_controls import state, stop
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize("route", ["gft:arm:lot:previous:0", "gft:confirmbind:previous"])
async def test_stale_gift_confirmation_opens_current_card_without_rearming(shop, route):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    await stop(r)
    before = r.store.rows("SELECT id,status FROM outbox")
    await state(r).set_data(
        {"gift_bind": {"item": "lot", "nonce": "previous"}, "gift_arm": {"item": "lot", "nonce": "previous"}}
    )
    await r.ui.click(route)
    assert (await state(r).get_data()).get("logical_view", {}).get("item") == "lot"
    assert any(key.startswith("lp:") for key in buttons(r.session.last_panel))
    assert r.store.rows("SELECT enabled FROM logical_products") == [{"enabled": 0}]
    assert r.store.rows("SELECT enabled FROM gift_listings") == [{"enabled": 0}]
    assert r.store.rows("SELECT id,status FROM outbox") == before
    assert not r.api.remote.payments
