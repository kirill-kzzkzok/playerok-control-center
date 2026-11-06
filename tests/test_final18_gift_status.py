import pytest

from app.gifts.service import GiftService
from app.services.orders import Orders
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize(
    "stopped,paused,unknown,expected",
    [
        (True, True, False, "остановлена владельцем"),
        (True, True, True, "остановлена владельцем"),
        (False, True, True, "Результат покупки неизвестен"),
        (False, True, False, "временная пауза"),
        (False, False, False, "разрешена для включённых товаров"),
    ],
)
async def test_home_uses_stop_safety_pause_precedence(shop, stopped, paused, unknown, expected):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    Orders(r.store).ingest(r.api.order("paid"))
    service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    assert service.discover() == 1
    if unknown:
        r.store.execute("UPDATE gift_jobs SET state='UNCERTAIN' WHERE order_id='paid'")
    r.store.set_setting("gifts:stopped", stopped)
    r.store.set_setting("gifts:paused", paused)
    before = r.store.rows("SELECT id,status FROM outbox")
    await r.ui.click("gft:home")
    assert expected in r.session.last_panel.text
    assert ("gft:stop" in buttons(r.session.last_panel)) == (not stopped)
    assert r.store.setting("gifts:stopped") is stopped
    assert r.store.setting("gifts:paused") is paused
    assert r.store.rows("SELECT id,status FROM outbox") == before
    assert not r.api.remote.payments and not r.api.messages
