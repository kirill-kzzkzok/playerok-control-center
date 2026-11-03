"""Diagnostic links reach the configuration that actually controls the product."""

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.services.product_readiness import ProductReadiness
from app.telegram.storage import SQLiteStorage
from tests.test_playerok_delivery import configure
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


def state(r):
    return FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))


async def test_write_permission_action_explains_env_without_a_fake_telegram_toggle(shop):
    r = await shop()
    await configure(r)
    mutations = len(r.api.mutations)
    r.api.account.writes_enabled = False
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    problem = next(p for p in result["problems"] if p["code"] == "WRITES_OFF")
    await r.ui.click(problem["action"][1])
    assert "PLAYEROK_WRITES_ENABLED" in r.session.last_panel.text
    assert "перезапустите" in r.session.last_panel.text
    assert "item:lot" in buttons(r.session.last_panel)
    assert not r.api.account.writes_enabled and len(r.api.mutations) == mutations


async def test_platform_projection_recovery_opens_platform_settings(shop):
    r = await shop()
    await configure(r)
    r.store.execute("UPDATE delivery_policies SET payload=json_set(payload,'$.enabled',0)")
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    problem = next(p for p in result["problems"] if p["code"] == "PROJECTION_MISMATCH")
    assert problem["action"][1] == "platform:item:lot"
    await r.ui.click(problem["action"][1])
    assert (await state(r).get_data())["platform"]["id"] == "lot"
    assert not r.api.messages and not r.api.remote.payments


async def test_individual_stars_limit_recovery_keeps_item_context(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    r.store.set_setting("gifts:listing_options:lot", {"order": 1, "day": 1})
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    problem = next(p for p in result["problems"] if p["code"] == "STARS_LIMIT")
    assert problem["action"][1] == "lp:limits:lot"
    await r.ui.click(problem["action"][1])
    data = await state(r).get_data()
    assert data["logical_view"]["item"] == "lot"
    assert any(key.endswith(":stop") for key in buttons(r.session.last_panel))
    await r.ui.click(next(key for key in buttons(r.session.last_panel) if key.endswith(":stop")))
    await r.ui.click("lp:limits:lot")
    assert any(key.endswith(":input:order") for key in buttons(r.session.last_panel))
    assert any(key.endswith(":input:day") for key in buttons(r.session.last_panel))
    assert not r.api.messages and not r.api.remote.payments


async def test_gift_settings_do_not_offer_emergency_stop_when_already_stopped(shop):
    r = await shop(gift=True)
    r.store.set_setting("gifts:stopped", True)
    await r.ui.click("gft:settings")
    assert "gft:stop" not in buttons(r.session.last_panel)
    assert "gft:resume" in buttons(r.session.last_panel)
