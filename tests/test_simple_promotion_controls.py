"""One seller budget input, existing explicit paid authority and no implicit charge."""

import pytest
from aiogram import Bot

from app.workers.promotions import PromotionWorker
from tests.test_paid_promotions import rig as rig_fixture
from tests.test_telegram_integration import Interaction, TelegramSession
from tests.test_telegram_transport import production_dispatcher
from tests.test_ux_redesign import buttons

rig = rig_fixture


async def test_disabled_promotion_has_enable_and_budget_then_explicit_money_consent(rig):
    r = rig
    r.settings.stop(7)
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, production_dispatcher(r.store, r.api))
    try:
        await ui.message("/start")
        await ui.click("promotions")
        visible = buttons(session.last_panel)
        assert "promo:enable" in visible
        assert "promo:budget" in visible
        assert "promo:stop" not in visible
        assert not any("Расписание" in text for text in visible.values())
        await ui.click("promo:budget")
        ui.dp = production_dispatcher(r.store, r.api)
        await ui.message("50")
        _, policy, _ = r.settings.get()
        assert policy.daily == "50.00" and policy.per_operation == "20.00"
        assert policy.monthly == "1000.00" and policy.max_operations_per_item == 10
        assert not r.store.setting("promotions:paid_authority")
        assert not r.api.charges and not r.store.rows("SELECT * FROM outbox")
        await ui.click("promo:enable")
        assert "Реальные расходы" in session.last_panel.text or "реальные расходы" in session.last_panel.text
        confirm = next(key for key in buttons(session.last_panel) if key.startswith("payp:confirmarm:"))
        assert not r.store.setting("promotions:paid_authority")
        await ui.click(confirm)
        authority = r.store.setting("promotions:paid_authority")
        assert authority["account_id"] == r.api.account.id
        await ui.click(confirm)
        assert r.store.setting("promotions:paid_authority") == authority
        assert not r.api.charges and not r.store.rows("SELECT * FROM outbox")
        await ui.click("promotions")
        assert "promo:stop" in buttons(session.last_panel)
        await ui.stop("promo:stop")
        assert not r.settings.get()[1].enabled
        assert not r.store.setting("promotions:paid_authority")
        await ui.click(confirm)
        assert not r.store.setting("promotions:paid_authority")
    finally:
        await session.close()


@pytest.mark.parametrize("unknown", [False, True])
async def test_simple_enable_uses_existing_quote_approval_worker_and_payment_quarantine(rig, unknown):
    r = rig
    r.api.items["item"].update(status="APPROVED", priority="PREMIUM")
    r.settings.stop(7)
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_my_items":
            return {"items": [r.api.items["item"]], "page_info": {"has_next_page": False}}
        return await original(method, **kwargs)

    r.api.call = call
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, production_dispatcher(r.store, r.api))
    try:
        await ui.message("/start")
        await ui.click("promotions")
        await ui.click("promo:enable")
        await ui.click(next(key for key in buttons(session.last_panel) if key.startswith("payp:confirmarm:")))
        schedule = PromotionWorker(r.store, r.api)
        await schedule.once()
        assert not r.store.rows("SELECT * FROM outbox")
        await ui.click("promotions")
        await ui.click("promo:catalog")
        await ui.click("payp:item:item")
        visible = buttons(session.last_panel)
        assert "Автоподнятие" in visible.values()
        assert any(text.startswith("Поднять за 10 ₽") for text in visible.values())
        await ui.click("payp:auto:item")
        await ui.click(next(key for key in buttons(session.last_panel) if key.startswith("payp:pick:")))
        assert "10.00 ₽" in session.last_panel.text
        await ui.click(next(key for key in buttons(session.last_panel) if key.startswith("payp:approve:")))
        assert not r.store.rows("SELECT * FROM outbox") and not r.api.charges
        await schedule.once()
        assert r.store.rows("SELECT kind,status FROM outbox") == [
            {"kind": "premium_bump", "status": "PENDING"}
        ]
        r.api.timeout = unknown
        await r.worker.once()
        assert len(r.api.charges) == 1
        assert r.store.rows("SELECT status FROM promotion_expenses") == [
            {"status": "UNCERTAIN" if unknown else "SUCCEEDED"}
        ]
        await ui.click("promotions")
        await ui.click("promo:expenses")
        assert "UNCERTAIN" not in session.last_panel.text and "SUCCEEDED" not in session.last_panel.text
        assert "PREMIUM product" in session.last_panel.text
        ui.dp = production_dispatcher(r.store, r.api)
        for _ in range(3):
            await schedule.once()
            await r.worker.once()
        assert len(r.api.charges) == 1
    finally:
        await session.close()


@pytest.mark.parametrize("failure", ["account", "stale", "zero", "invalid"])
async def test_budget_input_cannot_overwrite_changed_authority_or_save_invalid_limits(rig, failure):
    r = rig
    r.service.arm(7, r.settings.get()[0])
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, production_dispatcher(r.store, r.api))
    try:
        await ui.message("/start")
        await ui.click("promotions")
        await ui.click("promo:budget")
        if failure == "account":
            r.api.account.id = "other"
        elif failure == "stale":
            revision, policy, selection = r.settings.get()
            from dataclasses import asdict

            r.settings.save(7, revision, {**asdict(policy), "daily": "10"}, selection)
        before = r.store.rows("SELECT * FROM promotion_policy")
        authority = r.store.setting("promotions:paid_authority")
        await ui.message("0" if failure == "zero" else "NaN" if failure == "invalid" else "50")
        assert r.store.rows("SELECT * FROM promotion_policy") == before
        assert r.store.setting("promotions:paid_authority") == authority
        assert not r.api.charges and not r.store.rows("SELECT * FROM outbox")
    finally:
        await session.close()
