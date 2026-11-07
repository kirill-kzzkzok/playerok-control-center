from tests.test_ux_redesign import buttons
from tests.test_ux_redesign import ux as ux_fixture

ux = ux_fixture


async def test_unsaved_delivery_values_survive_stock_and_back_without_authority(ux):
    r = ux
    await r.ui.click("auto:item:lot")
    nonce = (await r.state.get_data())["automation"]["nonce"]
    await r.ui.click(f"ap:{nonce}:mode:stock")
    nonce = (await r.state.get_data())["automation"]["nonce"]
    await r.ui.click(f"ap:{nonce}:advanced")
    nonce = (await r.state.get_data())["automation"]["nonce"]
    await r.ui.click(f"ap:{nonce}:edit:greeting")
    await r.ui.message("Unsaved greeting")
    before = (await r.state.get_data())["automation"]
    assert before["values"]["greeting"] == "Unsaved greeting"
    await r.ui.click("stock:view:lot")
    back = next(k for k in buttons(r.session.last_panel) if k.startswith("nav:back:"))
    await r.ui.click(back)
    after = (await r.state.get_data())["automation"]
    assert after["values"] == before["values"]
    assert after["nonce"] != before["nonce"] and not after.get("arm_pending")
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
