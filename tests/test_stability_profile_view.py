"""Opening a verified profile is a view, not a request to stop account writes."""

import pytest
from aiogram import Bot

from app.playerok.adapter import APIError
from app.services.playerok_connection import PlayerokConnection
from tests.test_playerok_connection import rig as connection_fixture
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


@pytest.fixture
def connection(tmp_path):
    yield from connection_fixture.__wrapped__(tmp_path)


async def test_open_profile_retains_verified_identity_without_network_call(connection):
    store, api = connection
    checker = PlayerokConnection(store, api)
    await checker.check()
    checker.last_started = None  # the check cooldown has elapsed
    api.error = APIError("HTTP_503")
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, dispatcher(store, api))
    try:
        await ui.click("profile")
        assert api.calls == ["get"]
        assert api.account.connection_verified and api.account.writes_enabled
        assert api.account.automation_enabled
        assert checker.state["status"] == "CONNECTED"
        await ui.click("profile:check")
        assert api.calls == ["get", "get"]
        assert not api.account.connection_verified and not api.account.writes_enabled
        assert checker.state["status"] == "UNKNOWN"
    finally:
        await bot.session.close()
