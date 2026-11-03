"""Independent physical messages through production Dispatcher/FSM."""

import asyncio
from copy import deepcopy

import pytest

from app.database.store import now
from app.services.orders import Orders
from app.telegram.navigation_context import panel_key
from app.workers.notifications import Notifications
from tests.test_fix_navigation import ui as ui_fixture
from tests.test_telegram_transport import production_dispatcher

ui = ui_fixture


@pytest.mark.parametrize(
    "route",
    [
        "item:lot",
        "iv:delivery:lot",
        "lp:item:lot",
        "gft:settings",
        "stock:view:lot",
        "order:order",
        "ops:list:ALL:0",
        "result:list:0",
        "settings",
        "home",
    ],
)
async def test_production_routes_from_independent_message_leave_primary_untouched(ui, route):
    r = ui
    a = r.store.setting("navigation:123:7")["id"]
    before = deepcopy(r.session.messages[a])
    b = (await r.bot.send_message(7, "Independent card")).message_id
    await r.ui.click(route, message_id=b)
    assert r.session.messages[a] == before
    assert r.session.messages[b]["text"] != "Independent card"
    assert not r.store.rows("SELECT * FROM outbox")
    assert not any(
        call.__api_method__ in {"editMessageText", "editMessageMedia", "editMessageCaption"}
        and call.message_id == a
        for call in r.session.calls[2:]
    )


async def test_callback_back_and_settings_edit_only_their_own_card(ui):
    r = ui
    a = r.store.setting("navigation:123:7")["id"]
    b = (await r.bot.send_message(7, "Independent operation card")).message_id
    a_before = deepcopy(r.session.messages[a])
    await r.ui.click("ops:list:ALL:0", message_id=b)
    assert "История операций" in r.session.messages[b]["text"]
    assert r.session.messages[a] == a_before

    await r.ui.click("settings", message_id=b)
    b_before = deepcopy(r.session.messages[b])
    await r.ui.click("items", message_id=a)
    assert r.session.messages[b] == b_before
    markup = r.session.messages[b]["reply_markup"]
    back = next(
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data.startswith("nav:back:")
    )
    a_before = deepcopy(r.session.messages[a])
    await r.ui.click(back, message_id=b)
    assert "История операций" in r.session.messages[b]["text"]
    assert r.session.messages[a] == a_before


async def test_confirmation_context_survives_other_card_and_dispatcher_restart(ui):
    r = ui
    r.api.account.writes_enabled = True
    a = r.store.setting("navigation:123:7")["id"]
    await r.ui.click("editname:lot", message_id=a)
    await r.ui.message("New independent name")
    token = (await r.state.get_data())["confirm_token"]
    b = (await r.bot.send_message(7, "Operations")).message_id
    await r.ui.click("ops:list:ALL:0", message_id=b)
    b_before = deepcopy(r.session.messages[b])
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.click("commit:" + token, message_id=b)
    assert not r.store.rows("SELECT * FROM outbox")
    # Restore the read-only operations view after the rejected forged callback.
    await r.ui.click("ops:list:ALL:0", message_id=b)
    b_before = deepcopy(r.session.messages[b])
    await r.ui.click("commit:" + token, message_id=a)
    assert r.store.rows("SELECT kind,target FROM outbox") == [{"kind": "edit", "target": "lot"}], (
        r.session.messages[a]["text"]
    )
    assert r.session.messages[b] == b_before
    await r.ui.click("commit:" + token, message_id=a)
    assert len(r.store.rows("SELECT * FROM outbox")) == 1


async def test_parallel_read_routes_and_closed_stale_buttons_never_edit_neighbor(ui):
    r = ui
    a = r.store.setting("navigation:123:7")["id"]
    b = (await r.bot.send_message(7, "Operations")).message_id
    await r.ui.click("ops:list:ALL:0", message_id=b)
    await asyncio.gather(r.ui.click("items", message_id=a), r.ui.click("settings", message_id=b))
    assert "товар" in r.session.messages[a]["text"].lower()
    assert "Настройки" in r.session.messages[b]["text"]
    r.session.messages[b]["editable"] = False
    a_before = deepcopy(r.session.messages[a])
    await r.ui.click("ops:list:ALL:0", message_id=b)
    replacement = r.store.setting(panel_key(r.store, r.bot.id, 7, b))["id"]
    assert replacement != a and replacement != b
    assert r.session.messages[a] == a_before
    new_before = deepcopy(r.session.messages[replacement])
    await r.ui.click("settings", message_id=b)
    assert r.session.messages[a] == a_before
    assert r.session.messages[replacement] == new_before


async def test_historical_notification_refresh_cannot_overwrite_user_opened_screen(ui):
    r = ui
    b = (await r.bot.send_message(7, "Old notification")).message_id
    Orders(r.store).ingest_message(
        "chat", {"id": "old-notice", "text": "old buyer text", "created_at": now()}
    )
    r.store.execute(
        "INSERT INTO notifications(event_key,owner_id,status,telegram_message_id,rendered_payload) VALUES('message:old-notice',7,'SUCCEEDED',?,'old')",
        (b,),
    )
    await r.ui.click("ops:list:ALL:0", message_id=b)
    before = deepcopy(r.session.messages[b])
    Orders(r.store).ingest_message(
        "chat", {"id": "old-notice", "text": "changed buyer text", "created_at": now()}
    )
    await Notifications(r.store, r.bot).refresh_messages()
    assert r.session.messages[b] == before


async def test_open_chat_from_another_card_renders_source_without_touching_chat_notice(ui):
    r = ui
    b = (await r.bot.send_message(7, "Open chat here")).message_id
    before_ids = set(r.session.messages)
    await r.ui.click("chat:chat", message_id=b)
    assert "Переписка" in r.session.messages[b]["text"]
    assert set(r.session.messages) == before_ids
