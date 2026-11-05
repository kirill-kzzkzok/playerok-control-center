import threading

import pytest
from aiogram import Bot

from app.playerok.adapter import APIError
from app.telegram.keyboards import keyboard
from tests.test_paid_promotions import rig as rig_fixture
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher

rig = rig_fixture


@pytest.mark.parametrize("route", ["item:item", "payp:item:item", "order:missing", "home"])
async def test_source_markup_with_bot_session_lock_never_deepcopies_bot(rig, route):
    r = rig
    session = TelegramSession()
    session.live_lock = threading.RLock()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    original = r.api.call

    async def failing(method, **kwargs):
        if method in {"get_item", "get_deal", "get_delivery_deal"}:
            error = APIError("NETWORK_ERROR")
            error.read_only = True
            raise error
        return await original(method, **kwargs)

    r.api.call = failing
    try:
        markup = keyboard([[("К товару", "item:item")]], home=False)
        await ui.click(route, reply_markup=markup)
        assert session.last_panel.text
        if route != "home":
            assert "❗" in session.last_panel.text
        await ui.click("home", reply_markup=session.last_panel.reply_markup)
        assert session.last_panel.text
    finally:
        await session.close()
