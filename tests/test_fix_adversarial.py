"""Independent negative review of the phase-one fixes; production Dispatcher."""

import pytest

from tests.test_fix_navigation import ui as ui_fixture


@pytest.fixture(name="ui")
async def _ui(tmp_path):
    async for value in ui_fixture.__wrapped__(tmp_path):
        yield value


async def test_real_list_pagination_survives_lost_ack(ui):
    r = ui
    original = r.api.call

    async def pages(method, **kwargs):
        value = await original(method, **kwargs)
        if method == "get_my_items":
            value["page_info"] = {"has_next_page": not kwargs.get("after_cursor"), "end_cursor": "next-page"}
        return value

    r.api.call = pages
    await r.ui.click("items")
    next_action = next(
        b.callback_data
        for row in r.session.last_panel.reply_markup.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith("listnext:")
    )
    r.session.inject("answerCallbackQuery")
    await r.ui.click(next_action)
    assert any(
        method == "get_my_items" and args.get("after_cursor") == "next-page" for method, args in r.api.calls
    )
    assert not r.store.rows("SELECT * FROM outbox")


async def test_deleted_notification_cannot_commit_old_read_confirmation(ui):
    from app.services.orders import Orders
    from app.workers.notifications import Notifications

    r = ui
    r.api.account.writes_enabled = True
    r.store.set_setting("notifications:initialized", True)
    Orders(r.store).ingest_message(
        "chat", {"id": "incoming", "text": "hello", "user": {"id": "buyer", "username": "Buyer"}}
    )
    await Notifications(r.store, r.bot).once()
    ident = r.store.rows("SELECT telegram_message_id FROM notifications")[0]["telegram_message_id"]
    await r.ui.click("readchat:chat", message_id=ident)
    assert not (await r.state.get_data()).get("pending")
    Orders(r.store).ingest_message(
        "chat",
        {
            "id": "incoming",
            "text": "hello",
            "deleted_at": "2026-11-01T00:00:00Z",
            "user": {"id": "buyer", "username": "Buyer"},
        },
    )
    await r.ui.click("commit:obsolete")
    assert not r.store.rows("SELECT * FROM outbox")
