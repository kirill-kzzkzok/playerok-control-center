"""Paid placement via production services/Dispatcher, with fake Playerok transport only."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, Store, now
from app.playerok.adapter import APIError
from app.playerok.thread_calls import close_calls
from app.services.paid_promotions import PaidPromotions, intent_key
from app.services.products import Products, ValidationError
from app.services.promotion_settings import PromotionSettings
from app.services.promotions import PromotionPolicy
from app.services.reconciliation import Reconciliation, signature
from app.telegram.storage import SQLiteStorage
from app.workers.outbox import Executor, OutboxWorker
from app.workers.promotions import PromotionWorker
from tests.test_operations import FakeAccount
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


class PaidAPI:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.account = FakeAccount()
        self.account.automation_enabled = True
        self.account.connection_verified = True
        self.account.verified_account_id = "seller"
        self.items = {
            name: {
                "id": name,
                "user": {"id": "seller"},
                "name": "<PREMIUM product>",
                "price": 1000,
                "raw_price": 1000,
                "status": "DRAFT",
                "priority": "DEFAULT",
                "category": {"id": "category"},
                "priority_position": 25,
            }
            for name in ("item", "other")
        }
        self.quotes = [{"id": "premium", "type": "PREMIUM", "price": 10, "period": 7}]
        self.balance = 100
        self.calls = []
        self.charges = []
        self.timeout = False
        self.read_failure = False
        self.bad_receipt = False
        self.hook = None
        self.account.publish_item = self.charge
        self.account.increase_item_priority_status = self.charge

    def charge(self, **kwargs):
        if self.hook:
            self.hook()
        self.charges.append(kwargs)
        item = self.items[kwargs["item_id"]]
        item.update(
            status="APPROVED",
            priority="PREMIUM",
            status_payment={
                "id": "receipt-" + str(len(self.charges)),
                "status": "CONFIRMED",
                "direction": "OUT",
                "operation": "ITEM_PREMIUM_PRIORITY",
                "provider_id": "LOCAL",
                "value": 10,
            },
        )
        self.balance -= 10
        if self.timeout:
            raise TimeoutError()
        if self.bad_receipt:
            item["status_payment"]["status"] = "PENDING"
        return deepcopy(item)

    async def call(self, method, **kwargs):
        self.calls.append(method)
        if method == "get":
            return {"id": self.account.id, "profile": {"balance": {"available": self.balance}}}
        if method == "get_item":
            if self.read_failure and self.charges:
                raise APIError("NETWORK_ERROR")
            return deepcopy(self.items[kwargs["id"]])
        if method == "get_item_priority_statuses":
            return deepcopy(self.quotes)
        if method == "get_game_category":
            return {"id": "category", "options": []}
        raise AssertionError(method)


@pytest.fixture
async def rig(tmp_path):
    store, api = Store(tmp_path / "paid.sqlite"), PaidAPI()
    store.claim_first_owner(7)
    policy = PromotionPolicy(
        enabled=True, per_operation="20", daily="100", monthly="1000", max_operations_per_item=10
    )
    settings = PromotionSettings(store, api)
    settings.save(7, 0, asdict(policy), "selected")
    service = PaidPromotions(store, api)
    worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
    yield SimpleNamespace(store=store, api=api, service=service, settings=settings, worker=worker)
    await close_calls(api)
    store.close()


async def enqueue(r, ident="item", key="one", automatic=False):
    preview = await r.service.preview(7, ident, "premium")
    return await r.service.queue(7, preview, key, automatic=automatic)


@pytest.mark.parametrize("status", ["DRAFT", "APPROVED"])
async def test_confirmed_publish_and_bump_atomic_expense_and_no_duplicate(rig, status):
    r = rig
    r.api.items["item"]["status"] = status
    op = await enqueue(r)
    assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "RESERVED"}]
    await asyncio.gather(r.worker.once(), r.worker.once())
    assert len(r.api.charges) == 1
    row = r.store.rows("SELECT * FROM outbox WHERE id=?", (op,))[0]
    assert row["status"] == "SUCCEEDED"
    assert json.loads(row["result"])["paid_rub"] == "10.00"
    assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "SUCCEEDED"}]
    assert await r.service.queue(7, {}, "one") == op
    await r.worker.once()
    assert len(r.api.charges) == 1
    assert r.api.account.calls == ["publishItem" if status == "DRAFT" else "increaseItemPriorityStatus"]


@pytest.mark.parametrize("change", ["price", "status", "owner", "account", "quote", "policy", "stop"])
async def test_changes_before_dispatch_prevent_charge(rig, change):
    r = rig
    op = await enqueue(r)
    if change == "price":
        r.api.items["item"]["price"] = 999
    if change == "status":
        r.api.items["item"]["status"] = "SOLD"
    if change == "owner":
        r.api.items["item"]["user"]["id"] = "other"
    if change == "account":
        r.api.account.id = "other"
    if change == "quote":
        r.api.quotes[0]["price"] = 11
    if change == "policy":
        r.settings.save(7, 1, asdict(r.settings.get()[1]), "selected")
    if change == "stop":
        r.settings.stop(7)
    await r.worker.once()
    assert not r.api.charges
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "PERMANENT_FAILED"
    assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "FAILED"}]


async def test_price_change_before_confirm_and_insufficient_balance(rig):
    r = rig
    preview = await r.service.preview(7, "item", "premium")
    r.api.quotes[0]["price"] = 11
    with pytest.raises(Conflict):
        await r.service.queue(7, preview, "changed")
    r.api.quotes[0]["price"] = 10
    r.api.balance = 9
    with pytest.raises(ValidationError, match="баланса"):
        await enqueue(r)
    assert not r.store.rows("SELECT * FROM outbox") and not r.api.charges


async def test_atomic_budget_and_balance_reservations_concurrent_items(rig):
    r = rig
    r.api.balance = 15
    result = await asyncio.gather(enqueue(r, "item", "a"), enqueue(r, "other", "b"), return_exceptions=True)
    assert sum(isinstance(v, str) for v in result) == 1
    assert len(r.store.rows("SELECT * FROM outbox")) == 1
    assert not r.api.charges
    # With higher balance the same race must still obey the daily budget.
    r.api.balance = 100
    revision, policy, selection = r.settings.get()
    r.settings.save(7, revision, {**asdict(policy), "daily": "15"}, selection)
    with pytest.raises(ValidationError, match="дневной"):
        await enqueue(r, "other", "c")


async def test_lost_ack_is_uncertain_no_new_purchase_or_refund(rig):
    r = rig
    r.api.timeout = True
    op = await enqueue(r)
    await r.worker.once()
    assert len(r.api.charges) == 1
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "UNCERTAIN"
    assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "UNCERTAIN"}]
    with pytest.raises(Conflict):
        await enqueue(r, "other", "new")
    with pytest.raises(Conflict, match="потерян"):
        await Reconciliation(r.store, r.api).evidence(7, op)
    await r.worker.once()
    assert len(r.api.charges) == 1
    assert not r.store.rows("SELECT * FROM outbox WHERE kind='gift_refund'")


async def test_read_failure_after_ack_can_be_reconciled_without_payment(rig):
    r = rig
    r.api.read_failure = True
    op = await enqueue(r)
    await r.worker.once()
    assert len(r.api.charges) == 1
    r.api.read_failure = False
    service = Reconciliation(r.store, r.api)
    evidence = await service.evidence(7, op)
    assert evidence["payment_id"] == "receipt-1"
    row = r.store.rows("SELECT * FROM outbox WHERE id=?", (op,))[0]
    await service.resolve(7, op, signature(row), "CONFIRM_READ_STATE", "Платёж проверен по серверу")
    assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "SUCCEEDED"}]
    assert not r.store.setting(intent_key(op))["pending"]
    assert len(r.api.charges) == 1


async def test_pending_payment_not_success_even_when_priority_is_premium(rig):
    r = rig
    r.api.bad_receipt = True
    op = await enqueue(r)
    await r.worker.once()
    assert r.api.items["item"]["priority"] == "PREMIUM"
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "UNCERTAIN"
    assert len(r.api.charges) == 1


async def test_restart_after_intent_or_lease_loss_never_replays(rig):
    r = rig
    op = await enqueue(r)
    r.store.claim(lease_seconds=1)
    r.store.set_setting(intent_key(op), {"started_at": "before-crash", "pending": True})
    r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (op,))
    assert r.store.recover() == 1
    restarted = OutboxWorker(r.store, Executor(r.store, r.api, Products(r.store, r.api)))
    assert not await restarted.once()
    assert not r.api.charges
    assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "UNCERTAIN"}]
    with pytest.raises(Conflict):
        await enqueue(r, "other", "other")


async def test_restart_queued_before_intent_safe_to_execute_once(rig):
    r = rig
    await enqueue(r)
    restarted = OutboxWorker(r.store, Executor(r.store, r.api, Products(r.store, r.api)))
    await restarted.once()
    assert len(r.api.charges) == 1


async def test_legacy_dry_run_policy_no_spend_then_explicit_auto_and_stop(rig):
    r = rig
    item = r.api.items["item"]
    item.update(status="APPROVED", priority="PREMIUM")
    await r.settings.target(7, "item", "include")
    schedule = PromotionWorker(r.store, r.api)
    await schedule.once()
    assert not r.store.rows("SELECT * FROM outbox")
    preview = await r.service.preview(7, "item", "premium")
    await r.service.approve(7, preview)
    r.service.arm(7, 1)
    r.store.execute("UPDATE promotion_schedule SET due_at=0")
    await schedule.once()
    assert len(r.store.rows("SELECT * FROM outbox")) == 1 and not r.api.charges
    await r.worker.once()
    assert len(r.api.charges) == 1
    await schedule.once()
    assert len(r.store.rows("SELECT * FROM outbox")) == 1
    r.settings.stop(7)
    r.store.execute("UPDATE promotion_schedule SET due_at=0")
    await schedule.once()
    assert len(r.api.charges) == 1


async def test_two_workers_cannot_start_different_payments_concurrently(rig):
    r = rig
    await enqueue(r, "item", "first")
    await enqueue(r, "other", "second")
    # Durable in-flight intent from another process blocks, never authorizes a second request.
    first = r.store.rows("SELECT id FROM outbox WHERE operation_key='first'")[0]["id"]
    r.store.set_setting(intent_key(first), {"pending": True, "started_at": "active"})
    await r.worker.once(
        operation_id=r.store.rows("SELECT id FROM outbox WHERE operation_key='second'")[0]["id"]
    )
    assert not r.api.charges


async def test_dispatcher_preview_confirmation_restart_double_click_and_access(rig):
    r = rig
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("payp:item:item")
        choices = (await state.get_data())["paid_choices"]
        await ui.click(f"payp:pick:{choices['nonce']}:0")
        assert "10.00 ₽" in session.calls[-1].text
        pending = (await state.get_data())["paid_pending"]
        assert not r.store.rows("SELECT * FROM outbox")
        ui.dp = dispatcher(r.store, r.api)
        await ui.click("payp:commit:" + pending["nonce"], owner=8)
        assert not r.store.rows("SELECT * FROM outbox")
        await ui.click("payp:commit:" + pending["nonce"])
        await ui.click("payp:commit:" + pending["nonce"])
        assert len(r.store.rows("SELECT * FROM outbox")) == 1
        assert not r.api.charges
        await r.worker.once()
        op = r.store.rows("SELECT id FROM outbox")[0]["id"]
        await ui.click("result:show:" + op)
        assert "Подтверждено списание: 10.00 ₽" in session.calls[-1].text
    finally:
        await bot.session.close()


@pytest.mark.parametrize("item_status", ["DRAFT", "APPROVED"])
async def test_native_safeaccount_uses_exact_local_payment_mutation_and_readback(rig, item_status):
    import httpx

    from app.playerok.adapter import SafeAccount

    r = rig
    r.api.items["item"]["status"] = item_status
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        operation = "publishItem" if item_status == "DRAFT" else "increaseItemPriorityStatus"
        assert body["operationName"] == operation
        assert body["variables"]["input"]["transactionProviderId"] == "LOCAL"
        assert body["variables"]["input"]["priorityStatuses"] == ["premium"]
        r.api.charge(item_id="item")
        item = r.api.items["item"]
        raw = {
            "id": "item",
            "name": "Item",
            "price": 1000,
            "rawPrice": 1000,
            "user": {"id": "seller"},
            "status": "APPROVED",
            "priority": "PREMIUM",
            "statusPayment": {**item["status_payment"], "providerId": "LOCAL"},
        }
        return httpx.Response(200, json={"data": {operation: raw}})

    account = SafeAccount(
        "token=test", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    account.id = "seller"
    account.connection_verified = True
    account.verified_account_id = "seller"
    account.automation_enabled = True
    r.api.account = account
    try:
        await enqueue(r)
        await r.worker.once()
        assert len(seen) == 1
        assert r.store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
        await r.worker.once()
        assert len(seen) == 1
    finally:
        account.close()


@pytest.mark.parametrize("defect", ["old", "wrong-id", "wrong-cost", "wrong-direction", "wrong-provider"])
async def test_server_payment_evidence_must_be_independent_and_exact(rig, defect):
    r = rig
    before = r.api.items["item"]
    before["status_payment"] = {"id": "old-payment"}
    await enqueue(r)
    original = r.api.charge

    def charge(**kwargs):
        result = original(**kwargs)
        if defect == "old":
            result["status_payment"]["id"] = "old-payment"
            r.api.items["item"]["status_payment"]["id"] = "old-payment"
        if defect == "wrong-id":
            result["status_payment"]["id"] = "unrelated"
        if defect == "wrong-cost":
            r.api.items["item"]["status_payment"]["value"] = 20
        if defect == "wrong-direction":
            r.api.items["item"]["status_payment"]["direction"] = "IN"
        if defect == "wrong-provider":
            r.api.items["item"]["status_payment"]["provider_id"] = "BANK_CARD"
        return result

    r.api.account.publish_item = charge
    await r.worker.once()
    assert len(r.api.charges) == 1
    assert r.store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
    assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "UNCERTAIN"}]


async def test_explicit_paid_arming_and_tariff_confirmation_through_dispatcher(rig):
    r = rig
    r.api.items["item"].update(status="APPROVED", priority="PREMIUM")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("payp:arm")
        token = (await state.get_data())["paid_arm"]["nonce"]
        assert not r.store.setting("promotions:paid_authority")
        await ui.click("payp:confirmarm:" + token)
        assert r.store.setting("promotions:paid_authority")["revision"] == 1
        await ui.click("payp:item:item")
        view = (await state.get_data())["paid_choices"]
        await ui.click(f"payp:pick:{view['nonce']}:0")
        token = (await state.get_data())["paid_pending"]["nonce"]
        await ui.click("payp:approve:" + token)
        assert r.store.setting("promotions:approved:item")["amount"] == "10.00"
        assert r.settings.selected("item", "selected")
        assert not r.api.charges and not r.store.rows("SELECT * FROM outbox")
        await ui.click("promo:stop")
        assert not r.store.setting("promotions:paid_authority")
        await ui.click("payp:confirmarm:" + token)
        assert not r.store.setting("promotions:paid_authority")
    finally:
        await bot.session.close()


async def test_changed_per_item_tariff_never_uses_previous_auto_approval(rig):
    r = rig
    r.api.items["item"].update(status="APPROVED", priority="PREMIUM")
    await r.settings.target(7, "item", "include")
    await r.service.approve(7, await r.service.preview(7, "item", "premium"))
    r.service.arm(7, 1)
    r.api.quotes[0]["price"] = 11
    await PromotionWorker(r.store, r.api).once()
    assert not r.store.rows("SELECT * FROM outbox") and not r.api.charges


async def test_budget_is_rechecked_in_actual_dispatch_period(rig):
    r = rig
    op = await enqueue(r)
    # A queued reservation from a previous day must count against today's spend at dispatch.
    r.store.execute("UPDATE promotion_expenses SET created_at='2020-01-01T00:00:00+00:00'")
    r.store.execute("INSERT INTO promotion_expenses VALUES('external','other','95','SUCCEEDED',?)", (now(),))
    await r.worker.once()
    assert not r.api.charges
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "PERMANENT_FAILED"


async def test_item_changes_during_agreement_preparation_do_not_charge(rig):
    r = rig
    op = await enqueue(r)
    calls = 0
    original = r.api.call

    async def call(method, **kwargs):
        nonlocal calls
        if method == "get_item":
            calls += 1
            if calls == 2:
                r.api.items["item"]["price"] = 2000
        return await original(method, **kwargs)

    r.api.call = call
    await r.worker.once()
    assert not r.api.charges
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,))[0]["status"] == "PERMANENT_FAILED"


async def test_manual_stop_unknown_never_releases_uncertain_budget(rig):
    r = rig
    r.api.timeout = True
    op = await enqueue(r)
    await r.worker.once()
    row = r.store.rows("SELECT * FROM outbox WHERE id=?", (op,))[0]
    await Reconciliation(r.store, r.api).resolve(
        7, op, signature(row), "STOP_UNCERTAIN", "Остановлено без повторной покупки"
    )
    assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "UNCERTAIN"}]
    with pytest.raises(Conflict):
        await enqueue(r, "other", "no-retry")
    assert len(r.api.charges) == 1


async def test_recovered_before_intent_can_be_stopped_without_claiming_a_payment(rig):
    r = rig
    op = await enqueue(r)
    action = r.store.claim(lease_seconds=1)
    r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (op,))
    r.store.recover()
    service = Reconciliation(r.store, r.api)
    row = r.store.rows("SELECT * FROM outbox WHERE id=?", (op,))[0]
    await service.resolve(
        7, op, signature(row), "STOP_UNCERTAIN", "Запрос не начинался, выполнение остановлено"
    )
    assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "FAILED"}]
    with pytest.raises(ValidationError):
        await r.service.execute(r.worker.executor, action)
    assert not r.api.charges
    await enqueue(r, key="safe-new-preview")


async def test_stopped_paid_operation_can_still_be_reconciled_to_success(rig):
    r = rig
    r.api.timeout = True
    op = await enqueue(r)
    await r.worker.once()
    assert len(r.api.charges) == 1  # the server actually charged before the timeout
    # The lost response arrives late (journal intent), giving a receipt candidate.
    from app.services.paid_promotions import intent_key

    intent = r.store.setting(intent_key(op), {})
    r.store.set_setting(intent_key(op), {**intent, "candidate_id": "receipt-1"})
    service = Reconciliation(r.store, r.api)
    row = r.store.rows("SELECT * FROM outbox WHERE id=?", (op,))[0]
    await service.resolve(7, op, signature(row), "STOP_UNCERTAIN", "Пауза до ручной проверки квитанции")
    assert r.store.rows("SELECT status FROM promotion_expenses")[0]["status"] == "UNCERTAIN"
    # Later the owner finds the receipt on Playerok and re-confirms the stopped
    # operation: the expense closes as SUCCEEDED, the deadlock is escapable.
    r.api.timeout = False
    r.api.charges = []  # evidence() reads the item state; no new charge happens
    row = r.store.rows("SELECT * FROM outbox WHERE id=?", (op,))[0]
    await service.resolve(
        7, op, signature(row), "CONFIRM_READ_STATE", "Квитанция проверена лично на Playerok"
    )
    assert r.store.rows("SELECT status FROM promotion_expenses")[0]["status"] == "SUCCEEDED"
    assert not r.api.charges
