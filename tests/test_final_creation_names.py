"""Reserve the permanent-code suffix before the owner advances past name input."""

import pytest

from tests.test_final_modern_gift_creation import shop as shop_fixture

shop = shop_fixture


@pytest.mark.parametrize("name", ["x" * 67, "🧸" * 34])
async def test_new_wizard_refuses_names_that_cannot_fit_the_permanent_code(shop, name):
    r = shop
    await r.ui.message("/start")
    await r.ui.click("create")
    for _ in range(3):
        await r.wizard_action("choose:0")
    await r.ui.message("Передача по подтверждённому полю")
    ident = (await r.state.get_data())["draft_id"]
    assert r.store.get_draft(ident, 7)["payload"]["step"] == "name"
    await r.ui.message(name)
    assert r.store.get_draft(ident, 7)["payload"]["step"] == "name"
    assert "66" in r.session.calls[-1].text
    await r.ui.message("🧸" * 33)
    assert r.store.get_draft(ident, 7)["payload"]["step"] == "description"
    assert not r.api.created and not r.store.rows("SELECT * FROM logical_code_reservations")
