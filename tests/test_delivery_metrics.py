"""No fabricated paid timestamps: existing observations and independent receipts only."""

from app.gifts.worker import GiftWorker
from app.services.orders import Orders
from app.services.statistics import delivery_statistics
from tests.test_deal_snapshots import case as case_fixture
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

case = case_fixture
shop = shop_fixture


async def test_confirmed_gift_has_observed_latency_but_no_invented_server_payment_time(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart, heart")
    order = r.api.order("measured")
    Orders(r.store).ingest(order)
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert delivery_statistics(r.store)["completed"] == 0
    await r.worker.once()
    result = delivery_statistics(r.store)
    assert result["completed"] == 1
    assert result["server_paid"]["samples"] == 0
    assert result["observed"]["samples"] == 1
    assert result["observed"]["p50"] >= 0
    assert result["preparation"]["samples"] == 1
    assert result["confirmation"]["samples"] == 1
    assert result["recipient"]["samples"] == 2
    assert result["payment_form"]["samples"] == 2
    assert result["payment_rpc"]["samples"] == 2
    assert result["receipt_read"]["samples"] == 2


async def test_latency_screen_is_read_only_and_accessible_from_statistics(shop):
    from tests.test_ux_redesign import buttons

    r = await shop(gift=True)
    await r.ui.click("stats:7")
    assert "sr:7:delivery" in buttons(r.session.last_panel)
    await r.ui.click("sr:7:delivery")
    assert "нет подтверждённых измерений" in r.session.last_panel.text
    assert not r.api.remote.payments and not r.store.rows("SELECT * FROM outbox")
