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


@pytest.mark.parametrize("paused,unknown", [(False, False), (True, True)])
async def test_settings_stop_hides_duplicate_stop_even_with_enabled_projection(shop, paused, unknown):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    Orders(r.store).ingest(r.api.order("paid"))
    service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    assert service.discover() == 1
    if unknown:
        r.store.execute("UPDATE gift_jobs SET state='UNCERTAIN' WHERE order_id='paid'")
    r.store.set_setting("gifts:stopped", True)
    r.store.set_setting("gifts:paused", paused)
    projections = r.store.rows("SELECT enabled FROM gift_listings")
    assert projections == [{"enabled": 1}]
    before = r.store.rows("SELECT id,status FROM outbox")
    jobs = r.store.rows("SELECT * FROM gift_jobs")
    await r.ui.click("gft:settings")
    visible = buttons(r.session.last_panel)
    assert "gft:stop" not in visible
    assert "gft:resume" in visible
    assert "Экстренная остановка: да" in r.session.last_panel.text
    await r.ui.click("gft:resume")
    assert any(key.startswith("gft:resume_confirm:") for key in buttons(r.session.last_panel))
    assert r.store.setting("gifts:stopped") is True
    assert r.store.setting("gifts:paused") is paused
    assert r.store.rows("SELECT enabled FROM gift_listings") == projections
    assert r.store.rows("SELECT id,status FROM outbox") == before
    assert r.store.rows("SELECT * FROM gift_jobs") == jobs
    assert not r.api.remote.payments and not r.api.messages
