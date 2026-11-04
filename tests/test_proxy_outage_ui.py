"""Proxy loss before reads is an actionable UI error, never purchase authority."""

from dataclasses import replace
from types import SimpleNamespace

from app.localvpn import active_proxy
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import click
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_picker_proxy_loss_then_recovery_without_purchase_or_uncaught_exception(shop):
    r = await shop(gift=True)
    for route in ("items", "item:lot", "iv:delivery:lot", "lp:method:lot"):
        await click(r, action=route)
    action = next(k for k in buttons(r.session.last_panel) if k.endswith(":mode:telegram_gifts"))
    r.api.gift_sender.settings = replace(r.api.gift_sender.settings, telegram_transport="localvpn")
    proxy = SimpleNamespace(ready=False, state={"generation": 1})
    token = active_proxy.set(proxy)
    try:
        await r.ui.click(action)
        assert "прокси" in r.session.last_panel.text.lower()
        assert not r.api.remote.payments and not r.store.rows("SELECT * FROM outbox")
        proxy.ready = True
        await click(r, action="home")
        for route in ("items", "item:lot", "iv:delivery:lot", "lp:method:lot"):
            await click(r, action=route)
        await click(r, suffix=":mode:telegram_gifts")
        await click(r, suffix=":giftpick:heart")
        assert any(k.endswith(":giftdone") for k in buttons(r.session.last_panel)), r.session.last_panel.text
        assert not r.api.remote.payments and not r.store.rows("SELECT * FROM outbox")
    finally:
        active_proxy.reset(token)
