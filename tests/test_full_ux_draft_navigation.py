from tests.test_telegram_integration import existing_legacy_delivery_draft
from tests.test_ux_redesign import buttons
from tests.test_ux_redesign import ux as ux_fixture

ux = ux_fixture


async def test_unsaved_delivery_values_survive_stock_and_back_without_authority(ux):
    r = ux
    r.store.set_setting("playerok:account_id", r.api.account.id)
    await existing_legacy_delivery_draft(r.state, r.api.account.id, "lot")
    await r.ui.click("auto:item:lot")
    nonce = (await r.state.get_data())["logical_view"]["nonce"]
    await r.ui.click(f"lp:{nonce}:mode:unique_stock")
    await r.ui.click(f"lp:{nonce}:input:greeting")
    await r.ui.message("Unsaved greeting")
    before = (await r.state.get_data())["logical_setup"]
    assert before["config"]["greeting"] == "Unsaved greeting"
    old_nonce = (await r.state.get_data())["logical_view"]["nonce"]
    await r.ui.click("stock:view:lot")
    back = next(k for k in buttons(r.session.last_panel) if k.startswith("nav:back:"))
    await r.ui.click(back)
    assert (await r.state.get_data())["logical_setup"] == before
    assert (await r.state.get_data())["logical_view"]["nonce"] != old_nonce
    assert not (await r.state.get_data()).get("logical_prepared")
    assert not r.store.rows("SELECT * FROM delivery_policies")
    assert not r.store.rows("SELECT * FROM outbox")


async def test_stock_configuration_uses_the_product_delivery_entry(ux):
    r = ux
    r.store.set_setting("playerok:account_id", r.api.account.id)
    await r.ui.click("stock:view:lot")
    assert "iv:delivery:lot" in buttons(r.session.last_panel)
    assert "auto:item:lot" not in buttons(r.session.last_panel)
    await r.ui.click("iv:delivery:lot")
    assert "lp:method:lot" in buttons(r.session.last_panel)
    assert not r.store.rows("SELECT * FROM delivery_policies")
    assert not r.store.rows("SELECT * FROM outbox")


async def test_old_automation_button_for_unconfigured_product_opens_canonical_delivery(ux):
    r = ux
    r.store.set_setting("playerok:account_id", r.api.account.id)
    await r.ui.click("auto:item:lot")
    assert "lp:method:lot" in buttons(r.session.last_panel)
    assert not (await r.state.get_data()).get("automation")
    assert not r.store.rows("SELECT * FROM delivery_policies")
    assert not r.store.rows("SELECT * FROM outbox")
