"""Service input cleanup at the actual Dispatcher boundary, no live Telegram."""

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Document

from tests.test_ux_redesign import buttons
from tests.test_ux_redesign import ux as ux_fixture

ux = ux_fixture


@pytest.mark.parametrize(
    "route,value", [("il:search", "Item"), ("il:price", "not-a-number"), ("gft:limits", "20 100")]
)
async def test_only_accepted_fsm_input_is_deleted_once(ux, route, value):
    r = ux
    await r.ui.click(route)
    await r.ui.message(value)
    removed = [(m.chat_id, m.message_id) for m in r.session.calls if m.__api_method__ == "deleteMessage"]
    assert removed == ([] if value == "not-a-number" else [(7, r.ui.counter)])


async def test_media_input_is_removed_after_durable_staging(ux):
    r = ux
    await r.ui.click("editphoto:lot")
    await r.ui.click(next(key for key in buttons(r.session.last_panel) if key.endswith(":add")))
    await r.ui.message(
        document=Document(
            file_id="photo", file_unique_id="fixture", file_name="image.png", mime_type="image/png"
        )
    )
    assert any(m.__api_method__ == "deleteMessage" and m.message_id == r.ui.counter for m in r.session.calls)
    assert (await r.state.get_data()).get("pending")


async def test_delete_failure_does_not_lose_fsm_input_or_retry_a_mutation(ux):
    r = ux
    original = r.session.make_request

    async def transport(bot, method, timeout=None):
        if method.__api_method__ == "deleteMessage":
            raise TelegramNetworkError(method=method, message="test-only timeout")
        return await original(bot, method, timeout)

    r.session.make_request = transport
    await r.ui.click("gft:limits")
    await r.ui.message("20 100")
    assert (await r.state.get_data())["gift_limits"]["order"] == 20
    assert not r.store.rows("SELECT * FROM outbox")


async def test_ordinary_messages_commands_and_other_users_are_not_deleted(ux):
    r = ux
    await r.ui.message("ordinary message")
    await r.ui.click("il:search")
    await r.ui.message("/start")
    await r.ui.message("not-owner", owner=99)
    await r.ui.message("group input", group=True)
    assert not any(m.__api_method__ == "deleteMessage" for m in r.session.calls)
