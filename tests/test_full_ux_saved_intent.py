"""Dispatcher paths must display desired settings without deriving them from readiness."""

from app.services.automation import Automation
from app.services.logical_products import LogicalProducts
from tests.test_playerok_delivery import configure
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import click
from tests.test_unified_delivery_controls import state
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_stopped_platform_preserves_saved_reply_and_completion_after_open_save_reopen(shop):
    r = await shop()
    await configure(r, reply=True, fulfill=True, text="Saved buyer message")
    logical = LogicalProducts(r.store, r.api)
    logical.stop(7, logical.listing("lot")["id"])
    _, before = Automation(r.store, r.api).get("lot")
    assert before.paid_reply and before.mark_sent and not before.enabled
    await r.ui.click("platform:item:lot")
    opened = (await state(r).get_data())["platform"]
    assert opened["reply"] and opened["fulfill"] and opened["text"] == "Saved buyer message"
    await click(r, suffix=":save")
    await r.ui.click("platform:item:lot")
    after = (await state(r).get_data())["platform"]
    assert after["reply"] and after["fulfill"] and after["text"] == "Saved buyer message"
    assert not r.api.messages and not r.api.remote.payments


async def test_mass_promotion_stop_counts_includes_not_all_targets_and_works_offline(shop):
    from dataclasses import asdict

    from app.services.promotion_settings import PromotionSettings
    from app.services.promotions import PromotionPolicy

    r = await shop()
    settings = PromotionSettings(r.store, r.api)
    settings.save(7, 0, asdict(PromotionPolicy(enabled=True)), "selected")
    for ident, disposition in [
        ("included", "include"),
        ("excluded", "exclude"),
        ("other-excluded", "exclude"),
    ]:
        r.store.execute("INSERT INTO promotion_targets VALUES(?,?,7,'2026-11-06')", (ident, disposition))
    calls = len(r.api.calls)
    await r.ui.click("promo:stop")
    assert "Явно выбранных товаров: 1" in r.session.last_panel.text
    assert "Исключённых: 2" in r.session.last_panel.text
    assert len(r.api.calls) == calls
    await click(
        r, action=next(k for k in buttons(r.session.last_panel) if k.startswith("promo:stop_confirm:"))
    )
    assert not settings.get()[1].enabled
    assert len(r.store.rows("SELECT * FROM promotion_targets")) == 3
    assert len(r.api.calls) == calls
