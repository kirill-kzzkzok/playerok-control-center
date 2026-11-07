from types import SimpleNamespace

from app.database.store import Store
from app.services.paid_promotions import intent_key
from app.workers.promotions import PromotionWorker
from tests.test_paid_promotions import rig as rig_fixture
from tests.test_premium_receipt_fix import scenario

rig = rig_fixture


async def test_transient_identity_preflight_retains_payment_reserve_and_authority(rig):
    r = rig
    op, _ = await scenario(r)
    authority = r.store.setting("promotions:paid_authority", {})
    r.api.account.writes_enabled = False
    r.api.connection = SimpleNamespace(awaiting_verification=True)
    await r.worker.once(operation_id=op)
    row = r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (op,))[0]
    assert row == {"status": "RETRYABLE_FAILED", "started_at": None}
    assert r.store.rows("SELECT status FROM promotion_expenses")[0]["status"] == "RESERVED"
    assert r.store.setting("promotions:paid_authority", {}) == authority
    assert not r.api.charges


async def test_acknowledged_stale_receipt_rechecks_promptly_then_confirms_after_restart(rig):
    r = rig
    op, _ = await scenario(r, mutation="old", lag=99)
    await r.worker.once(operation_id=op)
    assert len(r.api.charges) == 1
    await r.service.reconcile_due()
    record = r.store.setting(intent_key(op))
    import time

    assert 0 < record["receipt_retry_at"] - time.time() <= 11
    restored = Store(r.store.path)
    try:
        r.api.call = type(r.api).call.__get__(r.api)
        record["receipt_retry_at"] = 0
        restored.set_setting(intent_key(op), record)
        await PromotionWorker(restored, r.api).once()
        assert restored.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "SUCCEEDED"}]
        assert len(r.api.charges) == 1
        assert restored.rows("SELECT status FROM promotion_expenses")[0]["status"] == "SUCCEEDED"
    finally:
        restored.close()


async def automatic_pair(r):
    for ident, code in [("item", "TXE"), ("other", "VPM")]:
        r.api.items[ident].update(
            status="APPROVED",
            priority="PREMIUM",
            sequence=100,
            name="Gift [" + code + "]",
            status_payment={"id": "old-" + ident},
        )
        await r.settings.target(7, ident, "include")
        await r.service.approve(7, await r.service.preview(7, ident, "premium"))
    r.service.arm(7, r.settings.get()[0])
    await PromotionWorker(r.store, r.api).once()
    for _ in range(2):
        await r.worker.once()


async def test_auto_results_are_one_short_notice_without_receipt_or_unknown_duplicates(rig):
    from aiogram import Bot

    from app.workers.notifications import Notifications
    from tests.test_telegram_integration import TelegramSession

    r = rig
    await automatic_pair(r)
    assert len(r.api.charges) == 2
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        await Notifications(r.store, bot).once()
        await Notifications(r.store, bot).once()
        notices = [m for m in session.calls if m.__api_method__ == "sendMessage"]
        assert len(notices) == 1
        assert "Автоподнятие" in notices[0].text
        assert "[TXE]" in notices[0].text and "[VPM]" in notices[0].text
        assert notices[0].text.count("успешно") == 2
        assert not notices[0].reply_markup.inline_keyboard
        assert "ID:" not in notices[0].text and "PREMIUM" not in notices[0].text
    finally:
        await bot.session.close()


async def test_stale_auto_receipt_updates_same_notice_after_safe_confirmation_with_stop(rig):
    import copy

    from aiogram import Bot

    from app.services.account_binding import bind_account
    from app.workers.notifications import Notifications
    from tests.test_telegram_integration import TelegramSession

    r = rig
    bind_account(r.store, "seller")
    original_charge = r.api.charge
    original_call = r.api.call
    visible = [False]

    def charge(**kwargs):
        result = original_charge(**kwargs)
        if kwargs["item_id"] == "other":
            operation = r.store.rows("SELECT id FROM outbox WHERE target='other' AND status='PROCESSING'")[0][
                "id"
            ]
            intent = r.store.setting(intent_key(operation))
            r.api.wallet = [
                {
                    **copy.deepcopy(result["status_payment"]),
                    "user": {"id": "seller"},
                    "created_at": intent["started_at"],
                }
            ]
            result["status_payment"] = {"id": "old-other"}
        return result

    async def read(method, **kwargs):
        result = await original_call(method, **kwargs)
        if method == "get_item" and kwargs["id"] == "other" and len(r.api.charges) == 2 and not visible[0]:
            result["status_payment"] = {"id": "old-other"}
        return result

    r.api.account.increase_item_priority_status = charge
    r.api.call = read
    await automatic_pair(r)
    assert r.store.rows("SELECT status FROM outbox WHERE target='other'") == [{"status": "UNCERTAIN"}]
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    notices = Notifications(r.store, bot)
    try:
        await notices.once()
        sent = [m for m in session.calls if m.__api_method__ == "sendMessage"]
        assert len(sent) == 1 and "проверяем списание" in sent[0].text
        assert "Неопределённый результат операции" not in sent[0].text
        assert sent[0].text.count("успешно") == 1
        visible[0] = True
        r.settings.stop(7)
        await PromotionWorker(r.store, r.api).once()
        await notices.once()
        edits = [m for m in session.calls if m.__api_method__ == "editMessageText"]
        assert len([m for m in session.calls if m.__api_method__ == "sendMessage"]) == 1
        assert edits and edits[-1].text.count("успешно") == 2
        assert not edits[-1].reply_markup.inline_keyboard
        assert len(r.api.charges) == 2 and not r.settings.get()[1].enabled
    finally:
        await notices.close()
        await bot.session.close()


async def test_good_category_position_is_reported_without_purchase(rig):
    from dataclasses import asdict

    from aiogram import Bot

    from app.workers.notifications import Notifications
    from tests.test_telegram_integration import TelegramSession

    r = rig
    revision, policy, selection = r.settings.get()
    r.settings.save(7, revision, {**asdict(policy), "mode": "position", "threshold": 35}, selection)
    for ident, position in [("item", 25), ("other", 434)]:
        r.api.items[ident].update(
            status="APPROVED", priority="PREMIUM", sequence=position, name="Lot " + ident
        )
        await r.settings.target(7, ident, "include")
    await PromotionWorker(r.store, r.api).once()
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    notices = Notifications(r.store, bot)
    try:
        await notices.once()
        text = session.last_panel.text
        assert "позиция 25" in text and "поднятие не требуется" in text and "434" in text
        assert not r.api.charges
    finally:
        await notices.close()
        await bot.session.close()


async def test_uncertain_telegram_summary_is_not_resent_and_payment_is_untouched(rig):
    from unittest.mock import AsyncMock

    from app.telegram.operation_results import OperationResults

    r = rig
    await automatic_pair(r)
    bot = SimpleNamespace(id=123, send_message=AsyncMock(side_effect=TimeoutError()))
    await OperationResults(r.store).notify(bot)
    await OperationResults(r.store).notify(bot)
    assert bot.send_message.await_count == 1
    assert r.store.setting("notifications:promotion-summary:7")["status"] == "UNCERTAIN"
    assert len(r.api.charges) == 2


async def test_summary_background_refresh_cannot_overwrite_its_opened_operation_card(rig):
    from aiogram import Bot

    from app.workers.notifications import Notifications
    from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher

    r = rig
    await automatic_pair(r)
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    notices = Notifications(r.store, bot)
    try:
        await notices.once()
        summary = r.store.setting("notifications:promotion-summary:7")["message_id"]
        op = r.store.rows("SELECT id FROM outbox WHERE target='item'")[0]["id"]
        ui = Interaction(bot, dispatcher(r.store, r.api))
        from app.telegram.keyboards import keyboard

        await ui.click(
            "ops:show:" + op,
            message_id=summary,
            reply_markup=keyboard([[("Проверить платёж", "ops:show:" + op)]], home=False),
        )
        before = len(bot.session.calls)
        r.settings.stop(7)
        await notices.once()
        assert not any(
            m.__api_method__ == "editMessageText" and m.message_id == summary
            for m in bot.session.calls[before:]
        )
        assert len(r.api.charges) == 2
    finally:
        await notices.close()
        await bot.session.close()


async def test_slow_summary_send_outlives_short_flush_budget_without_duplicate(rig):
    import asyncio
    from unittest.mock import AsyncMock

    import pytest

    from app.workers.notifications import Notifications

    r = rig
    await automatic_pair(r)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await finish.wait()
        return SimpleNamespace(message_id=1234)

    bot = SimpleNamespace(id=123, send_message=AsyncMock(side_effect=send))
    notices = Notifications(r.store, bot)
    try:
        with pytest.raises(TimeoutError):
            await notices.flush_receipts()
        assert entered.is_set()
        task = r.store.operation_receipt_flush_task
        assert not task.done()
        assert r.store.setting("notifications:promotion-summary:7")["status"] == "PROCESSING"
        finish.set()
        await notices.flush_receipts()
        await notices.flush_receipts()
        assert bot.send_message.await_count == 1
        assert r.store.setting("notifications:promotion-summary:7")["message_id"] == 1234
        assert len(r.api.charges) == 2
    finally:
        finish.set()
        await notices.close()


async def test_unstarted_auto_payment_keeps_standing_authority_after_transient_verification(rig):
    r = rig
    r.api.items["item"].update(status="APPROVED", priority="PREMIUM", sequence=100)
    await r.settings.target(7, "item", "include")
    await r.service.approve(7, await r.service.preview(7, "item", "premium"))
    r.service.arm(7, r.settings.get()[0])
    await PromotionWorker(r.store, r.api).once()
    op = r.store.rows("SELECT id FROM outbox")[0]["id"]
    authority = r.store.setting("promotions:paid_authority")
    assert authority
    r.api.account.writes_enabled = False
    r.api.connection = SimpleNamespace(awaiting_verification=True)
    await r.worker.once(operation_id=op)
    assert r.store.rows("SELECT status FROM outbox")[0]["status"] == "RETRYABLE_FAILED"
    assert r.store.setting("promotions:paid_authority") == authority
    r.api.account.writes_enabled = True
    r.api.connection.awaiting_verification = False
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (op,))
    await r.worker.once(operation_id=op)
    assert len(r.api.charges) == 1
    assert r.store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
    await r.worker.once(operation_id=op)
    assert len(r.api.charges) == 1


async def test_completed_failed_receipt_flush_does_not_poison_following_cycles(rig, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    import pytest

    from app.telegram.operation_results import OperationResults
    from app.workers.notifications import Notifications

    notices = Notifications(rig.store, SimpleNamespace())
    failure = AsyncMock(side_effect=RuntimeError("local read failed"))
    monkeypatch.setattr(OperationResults, "notify", failure)
    with pytest.raises(RuntimeError):
        await notices.flush_receipts()
    monkeypatch.setattr(OperationResults, "notify", AsyncMock())
    # The next cycle drains a failed task once; a subsequent cycle must recover.
    for _ in range(2):
        try:
            await notices.flush_receipts()
        except RuntimeError:
            await asyncio.sleep(0)
    assert OperationResults.notify.await_count == 1
    await notices.close()


async def test_promotion_summary_shows_exact_total_and_live_available_balance_once(rig, caplog):
    from aiogram import Bot

    from app.services.account_binding import bind_account
    from app.workers.notifications import Notifications
    from tests.test_telegram_integration import TelegramSession

    r = rig
    bind_account(r.store, "seller")
    await automatic_pair(r)
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    notices = Notifications(r.store, bot, r.api)
    caplog.set_level("INFO", logger="pcc.shop")
    before = r.api.calls.count("get")
    try:
        await notices.once()
        await notices.once()
        text = session.last_panel.text
        assert "Поднято лотов: 2 · −20 ₽ · доступно 80 ₽" in text
        assert r.api.calls.count("get") == before + 1
        assert len(r.api.charges) == 2
        assert sum("Поднято лотов: 2" in record.message for record in caplog.records) == 1
    finally:
        await notices.close()
        await bot.session.close()


async def test_promotion_summary_keeps_confirmed_total_when_balance_read_fails(rig):
    from aiogram import Bot

    from app.playerok.adapter import APIError
    from app.services.account_binding import bind_account
    from app.workers.notifications import Notifications
    from tests.test_telegram_integration import TelegramSession

    r = rig
    bind_account(r.store, "seller")
    await automatic_pair(r)
    call = r.api.call

    async def read(method, **kwargs):
        if method == "get":
            raise APIError("NETWORK_ERROR")
        return await call(method, **kwargs)

    r.api.call = read
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    notices = Notifications(r.store, bot, r.api)
    try:
        await notices.once()
        assert "Поднято лотов: 2 · −20 ₽ · остаток не подтверждён" in session.last_panel.text
        assert len(r.api.charges) == 2
        assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "SUCCEEDED"}] * 2
    finally:
        await notices.close()
        await bot.session.close()
