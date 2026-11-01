"""Choose gift quantity and order through real owner callbacks, no payments."""

from tests.test_telegram_transport import production_dispatcher
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_unified_delivery_controls import state
from tests.test_universal_delivery_e2e import click
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def open_picker(r):
    for route in ("items", "item:lot", "iv:delivery:lot", "lp:item:lot"):
        await click(r, action=route)
    await click(r, suffix=":configure")
    await click(r, suffix=":mode:telegram_gifts")


async def test_gift_buttons_keep_order_duplicates_and_survive_dispatcher_restart(shop):
    r = await shop(gift=True)
    await open_picker(r)
    assert "gift:" not in r.session.last_panel.text
    await click(r, suffix=":giftpick:heart")
    await click(r, suffix=":giftpick:heart")
    await click(r, suffix=":giftpick:rose")
    r.ui.dp = production_dispatcher(r.store, r.api)
    await click(r, suffix=":giftdone")
    data = await state(r).get_data()
    assert data["logical_setup"]["config"]["code"] == "heart, heart, rose"
    await click(r, suffix=":preview")
    prepared = (await state(r).get_data())["logical_prepared"]
    assert [g["code"] for g in prepared["config"]["bundle"]] == ["heart", "heart", "rose"]
    assert sum(g["stars"] for g in prepared["config"]["bundle"]) == 55
    assert not r.store.rows("SELECT * FROM outbox") and not r.api.remote.payments


async def test_picker_undo_and_count_limit_do_not_create_spending_authority(shop):
    r = await shop(gift=True)
    await open_picker(r)
    for _ in range(r.api.gift_settings.max_gifts):
        await click(r, suffix=":giftpick:heart")
    before = buttons(r.session.last_panel)
    action = next(key for key in before if key.endswith(":giftpick:heart"))
    await r.ui.click(action)
    assert len((await state(r).get_data())["logical_gift_selection"]) == r.api.gift_settings.max_gifts
    # Recover the current catalog after the rejected extra selection.
    await r.ui.click(action.rsplit(":", 2)[0] + ":catalog")
    await click(r, suffix=":giftundo")
    await click(r, suffix=":giftdone")
    assert (
        len((await state(r).get_data())["logical_setup"]["config"]["code"].split(","))
        == r.api.gift_settings.max_gifts - 1
    )
    assert not r.api.remote.payments


async def test_unavailable_selected_gift_is_not_shown_as_free(shop):
    r = await shop(gift=True)
    await open_picker(r)
    action = await click(r, suffix=":giftpick:heart")
    r.api.remote.catalog.pop("heart")
    await r.ui.click(action.rsplit(":", 2)[0] + ":catalog")
    assert "недоступен" in r.session.last_panel.text
    assert "0 ⭐" not in r.session.last_panel.text
