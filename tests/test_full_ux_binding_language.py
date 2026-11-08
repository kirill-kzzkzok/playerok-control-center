from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import click

shop = shop_fixture


async def test_pending_permanent_code_change_is_not_called_description_confirmation(shop):
    r = await shop()
    for route in ("items", "item:lot", "iv:delivery:lot", "lp:method:lot"):
        await click(r, action=route)
    await click(r, suffix=":mode:fixed_text")
    await r.ui.message("safe local text")
    await click(r, suffix=":preview")
    await click(r, suffix=":commit")
    before = r.store.rows("SELECT id,status FROM outbox")
    await r.ui.click("lp:item:lot")
    assert "Описание:" not in r.session.last_panel.text
    assert "Изменение лота:" in r.session.last_panel.text
    assert r.store.rows("SELECT id,status FROM outbox") == before
    assert not r.api.mutations and not r.api.messages
