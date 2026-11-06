import pytest

from tests.test_fix_observability import case as case_fixture
from tests.test_telegram_transport import production_dispatcher
from tests.test_ux_redesign import ux as ux_fixture

ux = ux_fixture
case = case_fixture


@pytest.mark.parametrize("route,value", [("il:price", "not-a-number"), ("discount:lot", "invalid")])
async def test_invalid_owner_input_is_not_deleted_or_saved(ux, route, value):
    r = ux
    if route.startswith("discount:"):
        r.api.value["raw_price"] = 100
    await r.ui.click(route)
    before = await r.state.get_data()
    await r.ui.message(value)
    assert not any(
        m.__api_method__ == "deleteMessage" and m.message_id == r.ui.counter for m in r.session.calls
    )
    assert await r.state.get_data() == before
    assert not r.store.rows("SELECT * FROM outbox")


async def test_sole_form_after_unrelated_card_and_restart_receives_plain_text(case):
    r = case
    a = r.store.setting("navigation:123:7")["id"]
    await r.ui.click("editname:lot", message_id=a)
    b = (await r.bot.send_message(7, "Other card")).message_id
    await r.ui.click("settings", message_id=b)
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.message("Intended new product title")
    rows = r.store.rows(
        "SELECT data FROM fsm WHERE state IS NULL AND json_extract(data,'$.pending.target')='lot'"
    )
    assert len(rows) == 1
    import json

    pending = json.loads(rows[0]["data"])["pending"]
    assert pending["kind"] == "edit" and "Intended new product title" in pending["payload"]["name"]
    assert not r.api.messages and not r.store.rows("SELECT * FROM outbox")
