from dataclasses import asdict

import pytest
from aiogram import Bot

from app.database.store import now
from app.services.premium_review import PremiumReview, blocking_expenses
from app.services.products import ValidationError
from app.services.reconciliation import Reconciliation, signature
from app.workers.promotions import PromotionWorker
from tests.test_paid_promotions import enqueue
from tests.test_paid_promotions import rig as rig_fixture
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher

rig = rig_fixture


async def approve_two(r):
    for ident in ("item", "other"):
        r.api.items[ident].update(status="APPROVED", priority="PREMIUM")
        await r.settings.target(7, ident, "include")
        await r.service.approve(7, await r.service.preview(7, ident, "premium"))
    r.service.arm(7, r.settings.get()[0])


@pytest.mark.parametrize("failure", ["multiple_quotes", "item_limit"])
async def test_other_item_keeps_purchasing_with_exact_approved_tariff(rig, failure):
    r = rig
    await approve_two(r)
    if failure == "multiple_quotes":
        r.api.quotes.append({**r.api.quotes[0], "id": "other-quote", "price": 18})
    else:
        revision, policy, selection = r.settings.get()
        r.settings.save(7, revision, {**asdict(policy), "max_operations_per_item": 1}, selection)
        await approve_two(r)
        r.store.execute("INSERT INTO promotion_expenses VALUES('old','item','10','SUCCEEDED',?)", (now(),))
    await PromotionWorker(r.store, r.api).once()
    assert r.settings.get()[1].enabled
    expected = ["item", "other"] if failure == "multiple_quotes" else ["other"]
    for _ in range(3):
        await r.worker.once()
    assert sorted(charge["item_id"] for charge in r.api.charges) == expected
    assert all(charge["priority_status_id"] == "premium" for charge in r.api.charges)


async def test_interval_edit_preserves_standing_permission_but_fences_old_queue(rig):
    r = rig
    await approve_two(r)
    old = await r.service.queue(7, await r.service.preview(7, "item", "premium"), "old", automatic=True)
    revision, policy, selection = r.settings.get()
    updated = r.settings.save(7, revision, {**asdict(policy), "interval_seconds": 7200}, selection)
    assert r.service.has_authority("other", updated)
    assert r.store.setting("promotions:approved:other")["policy_revision"] == updated
    await r.worker.once()
    assert not r.api.charges
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (old,)) == [{"status": "PERMANENT_FAILED"}]
    await PromotionWorker(r.store, r.api).once()
    for _ in range(3):
        await r.worker.once()
    assert sorted(c["item_id"] for c in r.api.charges) == ["item", "other"]


@pytest.mark.parametrize(
    "change,selection", [({"daily": "200"}, "selected"), ({"enabled": False}, "selected"), ({}, "all")]
)
async def test_financial_scope_or_stop_change_never_inherits_permission(rig, change, selection):
    r = rig
    await approve_two(r)
    revision, policy, _ = r.settings.get()
    updated = r.settings.save(7, revision, {**asdict(policy), **change}, selection)
    assert not r.service.has_authority("item", updated)
    assert r.store.setting("promotions:approved:item")["policy_revision"] == revision
    if selection == "selected":
        await PromotionWorker(r.store, r.api).once()
    await r.worker.once()
    assert not r.api.charges


@pytest.mark.parametrize("change", ["price", "missing", "duplicate"])
async def test_exact_approved_tariff_never_falls_back_or_expands_budget(rig, change):
    r = rig
    await approve_two(r)
    if change == "price":
        r.api.quotes[0]["price"] = 11
    elif change == "missing":
        r.api.quotes[0]["id"] = "new-quote"
    else:
        r.api.quotes.append(dict(r.api.quotes[0]))
    for _ in range(2):
        r.store.execute("UPDATE promotion_schedule SET due_at=0")
        await PromotionWorker(r.store, r.api).once()
        await r.worker.once()
    assert not r.api.charges and not r.store.rows("SELECT id FROM outbox")
    assert r.settings.get()[1].enabled


async def test_review_caps_survive_exact_later_receipt_reconciliation(rig):
    r = rig
    r.api.items["item"]["status"] = "APPROVED"
    r.api.items["other"]["status"] = "APPROVED"
    r.store.set_setting("playerok:account_id", "seller")
    r.api.timeout = True
    op = await enqueue(r)
    await r.worker.once()
    row = blocking_expenses(r.store)[0]
    r.api.wallet = [
        {**r.api.items["item"]["status_payment"], "created_at": row["started_at"], "user": {"id": "seller"}}
    ]
    review = PremiumReview(r.store, r.api)
    evidence = await review.observe(row)
    await review.approve(7, op, signature(row), evidence, r.settings.get()[0])
    intent = r.store.setting("promotions:intent:" + op)
    r.store.set_setting("promotions:intent:" + op, {**intent, "candidate_id": evidence["debit_id"]})
    service = Reconciliation(r.store, r.api)
    await service.resolve(
        7, op, signature(service.action(7, op)), "CONFIRM_READ_STATE", "Exact receipt subsequently available"
    )
    revision, policy, selection = r.settings.get()
    r.settings.save(7, revision, {**asdict(policy), "daily": "15"}, selection)
    with pytest.raises(ValidationError, match="дневной лимит"):
        await r.service.queue(7, await r.service.preview(7, "other", "premium"), "over-cap", manual=True)
    assert len(r.api.charges) == 1


async def test_promotion_screen_explains_each_waiting_item_instead_of_false_ready(rig):
    r = rig
    revision, policy, selection = r.settings.get()
    r.settings.save(7, revision, {**asdict(policy), "mode": "position", "threshold": 35}, selection)
    await approve_two(r)
    for ident in ("item", "other"):
        r.api.items[ident]["priority_position"] = 10
        r.api.items[ident]["sequence"] = 10
        r.api.items[ident]["name"] = "Product " + ident
    await PromotionWorker(r.store, r.api).once()
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        await Interaction(bot, dispatcher(r.store, r.api)).click("promotions")
        assert "Выбрано товаров: 2" in session.last_panel.text
        assert "Product item" in session.last_panel.text and "Product other" in session.last_panel.text
        assert "Позиция в категории: 10" in session.last_panel.text
        assert "позиция в категории ниже 35-й" in session.last_panel.text
        assert "позиция в PREMIUM" not in session.last_panel.text
        assert "Включено для подтверждённых товаров" not in session.last_panel.text
        assert not r.api.charges
    finally:
        await session.close()
