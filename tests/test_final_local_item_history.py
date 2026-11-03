"""Sold/private listings retain access to local receipts without public item reads."""

from app.playerok.adapter import APIError
from app.services.orders import Orders
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_history_opens_when_the_public_item_is_gone_and_keeps_receipts(shop):
    r = await shop()
    await setup_ui(r, "unique_stock", "secret-account")
    Orders(r.store).ingest(r.api.order("sold-order", status="CONFIRMED"))
    calls = []

    async def missing(method, **kwargs):
        calls.append(method)
        raise APIError("HTTP_404")

    r.api.call = missing
    before = r.store.rows("SELECT * FROM outbox")
    await r.ui.click("iv:history:lot")
    assert "order:sold-order" in buttons(r.session.last_panel)
    assert "локальн" in r.session.last_panel.text.lower()
    assert not calls and r.store.rows("SELECT * FROM outbox") == before
    assert not r.api.messages and not r.api.remote.payments
    await r.ui.click("item:lot")
    assert calls == ["get_item"]  # A current card still needs the real server.
