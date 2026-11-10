"""Owner promotion routes use one editor entry and one return action."""

from aiogram import Bot
from aiogram.types import InlineKeyboardButton

from tests.test_paid_promotions import rig as rig_fixture
from tests.test_telegram_integration import Interaction, TelegramSession
from tests.test_telegram_transport import production_dispatcher
from tests.test_ux_redesign import buttons

rig = rig_fixture


async def test_return_deduplication_preserves_a_pagination_button(rig, monkeypatch):
    from app.telegram.promotions_panel import PromotionsPanel

    original = PromotionsPanel.item_view

    def paged(panel, view):
        text, markup = original(panel, view)
        markup.inline_keyboard.insert(
            0, [InlineKeyboardButton(text="⬅️ Предыдущая страница", callback_data="promo:catalog")]
        )
        return text, markup

    monkeypatch.setattr(PromotionsPanel, "item_view", paged)
    rig.api.items["item"].update(status="APPROVED", priority="PREMIUM")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, production_dispatcher(rig.store, rig.api))
    try:
        await ui.message("/start")
        await ui.click("payp:item:item")
        await ui.click("promo:item:item")
        assert "promo:catalog" in buttons(session.last_panel)
    finally:
        await session.close()


async def test_nested_settings_back_returns_to_parent_without_competing_exits(rig):
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, production_dispatcher(rig.store, rig.api))
    try:
        await ui.message("/start")
        await ui.click("promotions")
        await ui.click(next(k for k in buttons(session.last_panel) if k.endswith(":advanced")))
        await ui.click(next(k for k in buttons(session.last_panel) if k.endswith(":group:time")))
        back = [(k, v) for k, v in buttons(session.last_panel).items() if v.startswith("⬅")]
        assert len(back) == 1 and back[0][1] == "⬅️ Назад"
        await ui.click(back[0][0])
        assert any(k.endswith(":group:time") for k in buttons(session.last_panel))
        assert not rig.api.charges
    finally:
        await session.close()


async def test_promotion_has_one_entry_and_one_back_in_nested_settings(rig):
    r = rig
    r.api.items["item"].update(status="APPROVED", priority="PREMIUM")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, production_dispatcher(r.store, r.api))
    try:
        await ui.message("/start")
        await ui.click("payp:item:item")
        visible = buttons(session.last_panel)
        assert list(visible.values()).count("Автоподнятие") == 1
        assert "Участие в автоподнятии" not in visible.values()
        await ui.click("promo:item:item")
        assert "payp:auto:item" in buttons(session.last_panel)
        await ui.click("promotions")
        await ui.click(next(k for k in buttons(session.last_panel) if k.endswith(":advanced")))
        await ui.click(next(k for k in buttons(session.last_panel) if k.endswith(":group:time")))
        back = [(k, v) for k, v in buttons(session.last_panel).items() if v.startswith("⬅")]
        assert len(back) == 1 and back[0][1] == "⬅️ Назад"
        assert any(k.endswith(":save") for k in buttons(session.last_panel))
        await ui.click(back[0][0])
        assert any(k.endswith(":group:time") for k in buttons(session.last_panel))
        assert not r.api.charges
    finally:
        await session.close()
