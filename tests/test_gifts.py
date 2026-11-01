"""Real Dispatcher + WAL + shared Outbox + native TL requests against local transports."""

import asyncio
import copy
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from telethon import errors, functions, types

from app.database.store import Conflict, Store, now
from app.gifts.core import SEED_GIFTS, GiftValidationError, buyer_recipient, gift_code_from_description
from app.gifts.mtproto import PersonalGiftSender
from app.gifts.service import GiftService
from app.gifts.settings import GiftSettings
from app.gifts.worker import GiftWorker
from app.playerok.adapter import APIError
from app.services.automation import Automation, DeliveryPolicy
from app.services.orders import Orders
from app.services.products import Products
from app.telegram.orders_panel import OrdersPanel
from app.telegram.storage import SQLiteStorage
from app.workers.outbox import Executor, OutboxWorker
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


class MTProtoTransport:
    def __init__(self):
        self.calls = []
        self.messages = []
        self.gift = types.StarGift(SEED_GIFTS["heart"][1], types.DocumentEmpty(1), 15, 13)
        self.stars = 200
        self.recipient = types.User(55, access_hash=987, username="Realbuyer")
        self.resolve_error = None
        self.pay_error = None
        self.changed_user = False
        self.stop_hook = None
        self.refunded_receipt = False
        self.form_price = 15
        self.allow_gift = True
        self.disallow_gifts = False
        self.resolve_count = 0

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return types.User(99, is_self=True)

    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request, functions.contacts.ResolveUsernameRequest):
            self.resolve_count += 1
            if self.resolve_error:
                raise self.resolve_error
            peer = types.PeerUser(66 if self.changed_user and self.resolve_count > 1 else self.recipient.id)
            return types.contacts.ResolvedPeer(
                peer,
                [],
                [self.recipient]
                if peer.user_id == 55
                else [types.User(66, access_hash=2, username="Realbuyer")],
            )
        if isinstance(request, functions.payments.GetStarGiftsRequest):
            return types.payments.StarGifts(0, [self.gift] if self.gift else [], [], [])
        if isinstance(request, functions.payments.GetStarsStatusRequest):
            return types.payments.StarsStatus(types.StarsAmount(self.stars, 0), [], [])
        if isinstance(request, functions.users.GetFullUserRequest):
            full = types.UserFull(
                request.id.user_id,
                types.PeerSettings(),
                types.PeerNotifySettings(),
                0,
                disallowed_gifts=types.DisallowedGiftsSettings(
                    disallow_unlimited_stargifts=self.disallow_gifts
                ),
            )
            return types.users.UserFull(full, [], [self.recipient])
        if isinstance(request, functions.payments.CheckCanSendGiftRequest):
            return (
                types.payments.CheckCanSendGiftResultOk()
                if self.allow_gift
                else types.payments.CheckCanSendGiftResultFail("restricted")
            )
        if isinstance(request, functions.payments.GetPaymentFormRequest):
            if self.stop_hook:
                self.stop_hook()
            return types.payments.PaymentFormStarGift(
                12345 + sum(isinstance(r, functions.payments.GetPaymentFormRequest) for r in self.calls),
                types.Invoice("XTR", [types.LabeledPrice("gift", self.form_price)]),
            )
        if isinstance(request, functions.payments.SendStarsFormRequest):
            msg = types.MessageService(
                id=len(self.messages) + 1,
                peer_id=types.PeerUser(request.invoice.peer.user_id),
                date=datetime.now(UTC),
                out=True,
                from_id=types.PeerUser(99),
                action=types.MessageActionStarGift(
                    self.gift, message=request.invoice.message, refunded=self.refunded_receipt
                ),
            )
            self.messages.append(msg)
            self.stars -= self.gift.stars
            if self.pay_error:
                raise self.pay_error
            return types.payments.PaymentResult(
                types.Updates([types.UpdateNewMessage(msg, 1, 1)], [], [], datetime.now(UTC), 1)
            )
        raise AssertionError(type(request).__name__)

    async def get_messages(self, peer, *, ids=None, limit=None):
        return [
            m for m in self.messages if m.peer_id.user_id == peer.user_id and (ids is None or m.id in ids)
        ]

    @property
    def payments(self):
        return [r for r in self.calls if isinstance(r, functions.payments.SendStarsFormRequest)]


class Marketplace:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.account = SimpleNamespace(
            id="shop",
            writes_enabled=True,
            automation_enabled=True,
            permit=lambda _: nullcontext(),
            send_message=self.send_message,
            update_deal=self.update_deal,
        )
        self.item = {
            "id": "item",
            "user": {"id": "shop", "role": "USER"},
            "status": "APPROVED",
            "description": "A real gift\ngift: heart",
            "category": {"id": "stars"},
            "obtaining_type": {"id": "gift"},
            "name": "Heart gift",
        }
        self.orders = {}
        self.sent = []
        self.refunds = []
        self.support_refund = False
        self.refund_error = False
        self.notice_error = False
        self.field_required = True

    def add_order(self, ident="order", username="@Realbuyer"):
        order = {
            "id": ident,
            "item": copy.deepcopy(self.item),
            "status": "PAID",
            "direction": "OUT",
            "has_problem": False,
            "is_automated": False,
            "chat": {"id": "chat-" + ident},
            "user": {"id": "playerok-buyer", "username": "WRONG_playerok_name"},
            "comment_from_buyer": "@WRONG_comment",
            "created_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            "obtaining_fields": [{"id": "username-field", "value": username}],
        }
        self.orders[ident] = order
        return order

    async def call(self, method, **kwargs):
        if method == "get_item":
            return copy.deepcopy(self.item)
        if method == "get_deal":
            return copy.deepcopy(self.orders[kwargs["deal_id"]])
        if method == "get_chat":
            return {"id": kwargs["chat_id"], "is_texting_allowed": True}
        raise AssertionError(method)

    async def pages(self, method, collection, **kwargs):
        assert method == "get_game_category_data_fields"
        yield [{"id": "username-field", "type": "OBTAINING_DATA", "required": self.field_required}]

    async def gift_refund_capability(self, order):
        # Explicit test-only server capability. Production Playerok exposes no equivalent.
        return self.support_refund

    def update_deal(self, deal_id, new_status):
        self.refunds.append(deal_id)
        self.orders[deal_id]["status"] = new_status.name
        if self.refund_error:
            raise APIError("CONNECTION_LOST", uncertain=True)
        return copy.deepcopy(self.orders[deal_id])

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        if self.notice_error:
            raise APIError("CONNECTION_LOST", uncertain=True)
        return {"id": "message-" + str(len(self.sent))}


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "gifts.sqlite")
    store.execute("INSERT INTO administrators VALUES(7)")
    store.set_setting("orders:last_complete_sync", now())
    store.set_setting("gifts:stopped", False)  # existing test-owner global permission
    api, remote = Marketplace(), MTProtoTransport()
    settings = GiftSettings(
        1, "test-hash", "data/test.session", "stars", "gift", "username-field", True, 100, 100, True
    )
    api.gift_settings = settings
    api.gift_sender = PersonalGiftSender(settings, client=remote)
    service = GiftService(store, api, settings, api.gift_sender)
    worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
    yield SimpleNamespace(store=store, api=api, remote=remote, service=service, worker=worker)
    store.close()


async def enroll(r, *, auto_fulfill=False, ident="order"):
    if not r.service.listing("item"):
        await r.service.bind(7, "item", "heart")
        l = r.service.listing("item")
        await r.service.arm(7, "item", l["revision"], auto_fulfill=auto_fulfill)
    Orders(r.store).ingest(r.api.add_order(ident))
    assert r.service.discover() == 1


async def test_shared_outbox_success_receipt_notice_and_automatic_completion(rig):
    r = rig
    await enroll(r, auto_fulfill=True)
    await r.worker.once()
    j = r.service.job("order")
    assert j["state"] == "SUCCEEDED" and j["recipient_id"] == 55 and j["message_id"] == 1
    assert len(r.remote.payments) == 1 and r.remote.payments[0].invoice.peer.user_id == 55
    assert r.remote.payments[0].invoice.gift_id == SEED_GIFTS["heart"][1]
    assert not r.api.sent and r.api.orders["order"]["status"] == "PAID"
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()  # customer notice
    await r.worker.once()  # existing Fulfillment, after proof
    assert len(r.api.sent) == 1 and r.api.orders["order"]["status"] == "SENT"
    assert r.store.rows("SELECT status FROM outbox WHERE kind='fulfill'")[0]["status"] == "SUCCEEDED"
    Orders(r.store).ingest(r.api.orders["order"])
    assert r.service.discover() == 0 and len(r.remote.payments) == 1


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("balance", "BALANCE_TOO_LOW"),
        ("catalog", "GIFT_UNAVAILABLE"),
        ("price", "PRICE_CHANGED"),
        ("form_price", "PAYMENT_FORM_MISMATCH"),
        ("channel", "RECIPIENT_NOT_USER"),
        ("changed", "RECIPIENT_CHANGED"),
        ("temporary", "GIFT_CHECK_FAILED"),
        ("format", "GIFT_VALIDATION_FAILED"),
        ("missing", "GIFT_VALIDATION_FAILED"),
        ("restricted", "GIFT_RESTRICTED"),
        ("stop", "AUTOMATION_STOPPED_OR_STALE"),
        ("description", "LISTING_CHANGED"),
        ("owner", "GIFT_VALIDATION_FAILED"),
    ],
)
async def test_preflight_failures_never_spend_or_refund(rig, fault, expected):
    r = rig
    await enroll(r)
    if fault == "balance":
        r.remote.stars = 0
    elif fault == "catalog":
        r.remote.gift = None
    elif fault == "price":
        r.remote.gift.stars = 16
    elif fault == "form_price":
        r.remote.form_price = 16
    elif fault == "changed":
        r.remote.changed_user = True
    elif fault == "temporary":
        r.remote.resolve_error = TimeoutError()
    elif fault == "format":
        r.api.orders["order"]["obtaining_fields"][0]["value"] = "@bad!"
    elif fault == "missing":
        r.api.orders["order"]["obtaining_fields"] = []
    elif fault == "restricted":
        r.remote.allow_gift = False
    elif fault == "stop":
        r.remote.stop_hook = lambda: r.service.stop(7)
    elif fault == "description":
        r.api.item["description"] = "Changed\ngift: heart"
    elif fault == "owner":
        r.api.orders["order"]["item"]["user"]["id"] = "other-shop"
    elif fault == "channel":
        original = r.remote.__class__.__call__

        async def channel(request):
            if isinstance(request, functions.contacts.ResolveUsernameRequest):
                return types.contacts.ResolvedPeer(types.PeerChannel(55), [], [])
            return await original(r.remote, request)

        r.api.gift_sender.client = SimpleNamespace(connect=r.remote.connect, disconnect=r.remote.disconnect)

        # Connect already happened while binding; keep the authenticated sender and replace RPC adapter.
        class ChannelClient:
            __call__ = staticmethod(channel)

        r.api.gift_sender.client = ChannelClient()
    await r.worker.once()
    j = r.service.job("order")
    assert j["state"] == "BLOCKED" and j["error_code"] == expected
    assert not r.remote.payments and not r.api.refunds and j["refund_state"] == "NONE"


@pytest.mark.parametrize("error", [errors.UsernameInvalidError(None), errors.UsernameNotOccupiedError(None)])
@pytest.mark.parametrize("supported", [False, True])
async def test_definitive_username_failure_refund_or_honest_manual_fallback(rig, error, supported):
    r = rig
    await enroll(r)
    r.api.support_refund = supported
    r.remote.resolve_error = error
    await r.worker.once()
    j = r.service.job("order")
    assert j["state"] == "INVALID_RECIPIENT" and not r.remote.payments
    assert j["refund_state"] == ("QUEUED" if supported else "MANUAL")
    if supported:
        await r.worker.once()
        assert r.service.job("order")["refund_state"] == "CONFIRMED"
        assert r.api.orders["order"]["status"] == "ROLLED_BACK"
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert ("Возврат подтверждён" in r.api.sent[0][1]) is supported
    assert len(r.api.refunds) == int(supported)


async def test_lost_payment_ack_restart_reconcile_without_second_form_or_refund(rig):
    r = rig
    await enroll(r)
    r.remote.pay_error = TimeoutError()
    await r.worker.once()
    assert r.service.job("order")["state"] == "UNCERTAIN"
    count = len(r.remote.calls)
    r.store.close()
    r.store = Store(r.store.path)
    r.service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    assert r.store.recover() == 0
    assert r.service.discover() == 0
    assert r.service.job("order")["state"] == "UNCERTAIN"
    await r.service.reconcile(7, "order")
    assert r.service.job("order")["state"] == "SUCCEEDED"
    assert len(r.remote.calls) == count and len(r.remote.payments) == 1
    assert not r.api.refunds
    r.store.close()


@pytest.mark.parametrize("stage", ["checking", "spending", "after-send"])
async def test_expired_lease_restart_has_no_replay(rig, stage):
    r = rig
    await enroll(r)
    action = r.store.claim(lease_seconds=1)
    if stage == "checking":
        r.store.execute("UPDATE gift_jobs SET state='CHECKING'")
    else:
        r.store.execute("UPDATE gift_jobs SET state='SPENDING',spent_at=?,stars=15,form_id='1'", (now(),))
    r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (action["id"],))
    assert r.store.recover() == 1
    assert r.service.job("order")["state"] == ("BLOCKED" if stage == "checking" else "UNCERTAIN")
    assert await r.worker.once() is False and not r.remote.payments


async def test_duplicate_paid_and_concurrent_outbox_workers_charge_once(rig):
    r = rig
    await enroll(r)
    await asyncio.gather(
        asyncio.to_thread(Orders(r.store).ingest, r.api.orders["order"]),
        asyncio.to_thread(Orders(r.store).ingest, r.api.orders["order"]),
    )
    assert r.service.discover() == 0
    await asyncio.gather(r.worker.once(), r.worker.once())
    assert len(r.remote.payments) == 1 and r.service.job("order")["state"] == "SUCCEEDED"


async def test_two_orders_and_daily_limit_persist(rig):
    r = rig
    await enroll(r)
    await enroll(r, ident="order2")
    r.service.set_limits(7, 15, 15)
    await r.worker.once()
    await r.worker.once()
    assert len(r.remote.payments) == 1
    assert {r.service.job(i)["state"] for i in ["order", "order2"]} == {"SUCCEEDED", "BLOCKED"}
    assert r.store.setting("gifts:limits") == {"order": 15, "day": 15}


async def test_lost_refund_ack_is_read_reconciled_never_retried(rig):
    r = rig
    await enroll(r)
    r.api.support_refund = True
    r.api.refund_error = True
    r.remote.resolve_error = errors.UsernameNotOccupiedError(None)
    await r.worker.once()
    await r.worker.once()
    assert r.service.job("order")["refund_state"] == "UNCERTAIN"
    assert len(r.api.refunds) == 1 and not r.remote.payments
    await r.service.reconcile_refund(7, "order")
    assert r.service.job("order")["refund_state"] == "CONFIRMED"
    assert len(r.api.refunds) == 1


async def test_notice_failure_does_not_repeat_gift(rig):
    r = rig
    await enroll(r)
    await r.worker.once()
    r.api.notice_error = True
    w = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await w.once()
    await r.worker.once()
    await w.once()
    assert await r.worker.once() is False
    assert r.service.job("order")["state"] == "SUCCEEDED" and len(r.remote.payments) == 1
    assert r.store.rows("SELECT status FROM outbox WHERE kind='gift_notice'")[0]["status"] == "UNCERTAIN"


@pytest.mark.parametrize("status", ["PENDING", "ROLLED_BACK", "SENT"])
async def test_fresh_non_paid_order_never_sends(rig, status):
    r = rig
    await enroll(r)
    r.api.orders["order"]["status"] = status
    await r.worker.once()
    assert not r.remote.payments and not r.api.refunds
    assert r.service.job("order")["state"] == "BLOCKED"


async def test_receiver_privacy_blocks_before_payment(rig):
    r = rig
    await enroll(r)
    r.remote.disallow_gifts = True
    await r.worker.once()
    assert not r.remote.payments and r.service.job("order")["error_code"] == "RECIPIENT_GIFTS_FORBIDDEN"


@pytest.mark.parametrize("defect", ["refunded", "wrong-gift", "wrong-peer", "wrong-reference", "read-failed"])
async def test_payment_ack_alone_is_not_delivery_evidence(rig, defect):
    r = rig
    await enroll(r)
    original = r.remote.get_messages

    async def bad_read(peer, **kwargs):
        if defect == "read-failed":
            raise TimeoutError()
        messages = copy.deepcopy(await original(peer, **kwargs))
        for msg in messages:
            if defect == "refunded":
                msg.action.refunded = True
            elif defect == "wrong-gift":
                msg.action.gift.id += 1
            elif defect == "wrong-peer":
                msg.peer_id = types.PeerUser(101)
            else:
                msg.action.message.text = "unrelated"
        return messages

    r.remote.get_messages = bad_read
    await r.worker.once()
    assert len(r.remote.payments) == 1 and r.service.job("order")["state"] == "UNCERTAIN"
    Orders(r.store).ingest(r.api.add_order("order2"))
    assert r.service.discover() == 0  # global stop for all gifts until reliable read evidence
    with pytest.raises(Conflict):
        r.service.retry_preflight(7, "order")
    assert not r.api.refunds


async def test_account_switch_blocks_payment_and_receipt_adoption(rig):
    r = rig
    await enroll(r)
    r.api.gift_sender.me = types.User(100, is_self=True)
    await r.worker.once()
    assert not r.remote.payments and r.service.job("order")["error_code"] == "PERSONAL_ACCOUNT_CHANGED"


async def test_two_concurrent_orders_payment_lock_and_explicit_safe_retry(rig):
    r = rig
    await enroll(r)
    await enroll(r, ident="order2")
    entered, release = asyncio.Event(), asyncio.Event()
    pay = r.api.gift_sender.pay

    async def slow_pay(prepared):
        entered.set()
        await release.wait()
        return await pay(prepared)

    r.api.gift_sender.pay = slow_pay
    first = asyncio.create_task(r.worker.once())
    await entered.wait()
    await r.worker.once()
    assert r.service.job("order2")["error_code"] == "PAYMENT_LOCKED"
    release.set()
    await first
    r.service.retry_preflight(7, "order2")
    await r.worker.once()
    assert len(r.remote.payments) == 2
    assert all(r.service.job(i)["state"] == "SUCCEEDED" for i in ["order", "order2"])


@pytest.mark.parametrize("after_send", [False, True])
async def test_cancellation_at_paid_boundary_never_replays(rig, after_send):
    r = rig
    await enroll(r)
    entered = asyncio.Event()
    pay = r.api.gift_sender.pay

    async def interrupted(prepared):
        if after_send:
            await pay(prepared)
        entered.set()
        await asyncio.Event().wait()

    r.api.gift_sender.pay = interrupted
    task = asyncio.create_task(r.worker.once())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert r.service.job("order")["state"] == "UNCERTAIN"
    assert await r.worker.once() is False
    assert len(r.remote.payments) == int(after_send)


async def test_restart_during_recipient_check_retains_blocked_intent(rig):
    r = rig
    await enroll(r)
    entered = asyncio.Event()

    async def blocked_resolve(username):
        entered.set()
        await asyncio.Event().wait()

    r.api.gift_sender.resolve = blocked_resolve
    task = asyncio.create_task(r.worker.once())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert r.service.job("order")["state"] == "BLOCKED" and not r.remote.payments
    assert r.service.discover() == 0


async def test_dispatcher_uncertain_read_reconciliation_and_owner_notice(rig):
    from app.telegram.operation_results import OperationResults

    r = rig
    await enroll(r)
    r.remote.pay_error = TimeoutError()
    await r.worker.once()
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    try:
        await OperationResults(r.store).notify(bot)
        assert "Результат неизвестен" in session.calls[-1].text
        await ui.click("gft:list:uncertain:0")
        await ui.click("gft:job:order")
        assert "Realbuyer" not in session.calls[-1].text
        ui.dp = dispatcher(r.store, r.api)
        await ui.click("gft:reconcile:order")
        assert "Выполнено и подтверждено" in session.calls[-1].text and len(r.remote.payments) == 1
        await ui.click("gft:stop")
        await ui.click("gft:home", owner=8)
        assert r.store.setting("gifts:stopped") is True
    finally:
        await bot.session.close()


async def test_stock_and_gifts_exclude_each_other(rig):
    r = rig
    await enroll(r)
    Orders(r.store).add_stock("item", ["secret stock"])
    with pytest.raises(Conflict):
        Orders(r.store).reserve("order", actor=7)
    from dataclasses import asdict

    with pytest.raises(Conflict):
        await Automation(r.store, r.api).save(
            7, "item", 0, asdict(DeliveryPolicy(enabled=True, mode="stock"))
        )
    with pytest.raises(Conflict):
        r.store.enqueue("bad-delivery", "delivery", "order", {})


async def test_dispatcher_bind_restart_confirm_limits_enable_stop_and_order_results(rig):
    r = rig
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("gft:home")
        assert "Telegram-подарки" in session.calls[-1].text
        await ui.click("gft:bind:item")
        p = (await state.get_data())["gift_bind"]
        ui.dp = dispatcher(r.store, r.api)
        await ui.click("gft:confirmbind:" + p["nonce"])
        assert r.service.listing("item")["enabled"] == 0
        await ui.click("gft:limits")
        ui.dp = dispatcher(r.store, r.api)
        await ui.message("15 30")
        data = (await state.get_data())["gift_limits"]
        await ui.click("gft:save_limits:" + data["nonce"])
        assert r.service.limits() == (15, 30)
        await ui.click("gft:enable:item")
        p = (await state.get_data())["gift_arm"]
        ui.dp = dispatcher(r.store, r.api)
        await ui.click(f"gft:arm:item:{p['nonce']}:0")
        assert r.service.listing("item")["enabled"] == 1
        await ui.click(f"gft:arm:item:{p['nonce']}:0")  # consumed nonce
        Orders(r.store).ingest(r.api.add_order())
        r.service.discover()
        await r.worker.once()
        await ui.click("gft:list:success:0")
        await ui.click("gft:job:order")
        assert (
            "Выполнено и подтверждено" in session.calls[-1].text and "Realbuyer" not in session.calls[-1].text
        )
        text, _ = OrdersPanel(r.store, r.api).delivered(7)
        assert "Отправка подтверждена" in text
        await ui.click("gft:stop")
        assert r.service.listing("item")["enabled"] == 0 and r.store.setting("gifts:stopped") is True
        assert len(r.remote.payments) == 1
    finally:
        await bot.session.close()


@pytest.mark.parametrize(
    "description",
    ["gift: heart\ntext", "gift: heart\ngift: heart", "gift: unknown", "gift: !!!\ngift: heart"],
)
def test_tags_fail_closed(description):
    with pytest.raises(GiftValidationError):
        gift_code_from_description(description)


def test_exact_receiving_field_only_no_label_comment_or_automation_fallback():
    order = {
        "user": {"username": "wrong"},
        "comment_from_buyer": "@Wrongname",
        "automation_obtaining_fields": [{"code": "username-field", "value": "@Wrongname"}],
        "obtaining_fields": [
            {"id": "other", "label": "Telegram username", "value": "@Wrongname"},
            {"id": "username-field", "value": "@Realbuyer"},
        ],
    }
    assert buyer_recipient(order, field_id="username-field") == "@Realbuyer"
    for field in ["", "missing"]:
        with pytest.raises(GiftValidationError):
            buyer_recipient(order, field_id=field)


def test_defaults_off_and_secrets_repr_hidden(tmp_path):
    s = GiftSettings.load(str(tmp_path / "missing.env"))
    assert s.payment_enabled is False
    assert "test-hash" not in repr(replace(s, api_hash="test-hash"))


async def test_description_edit_queue_stops_gifts_before_remote_write(rig):
    r = rig
    await enroll(r)
    r.store.enqueue(
        "description-edit", "edit", "item", {"description": "New description\ngift: heart"}, actor=7
    )
    assert r.service.listing("item")["enabled"] == 0
    await r.worker.once()
    assert not r.remote.payments


async def test_dispatcher_rejects_price_changed_since_binding_preview(rig):
    r = rig
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("gft:bind:item")
        pending = (await state.get_data())["gift_bind"]
        r.remote.gift.stars = 20
        await ui.click("gft:confirmbind:" + pending["nonce"])
        assert not r.service.listing("item")
        assert "Цена изменилась" in session.calls[-1].text
    finally:
        await bot.session.close()


async def test_dispatcher_wizard_gift_description_validation_survives_restart(rig):
    r = rig
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    ident = r.store.draft(
        7, {"step": "description", "category_id": "stars", "obtaining_type_id": "gift", "photos": []}
    )
    await state.set_state("wizard")
    await state.set_data({"draft_id": ident, "revision": 0})
    try:
        await ui.message("Valid description without gift marker")
        assert r.store.get_draft(ident, 7)["payload"]["step"] == "description"
        ui.dp = dispatcher(r.store, r.api)
        await ui.message("Valid description\ngift: heart")
        assert r.store.get_draft(ident, 7)["payload"]["step"] == "photos"
        assert not r.store.rows("SELECT * FROM gift_listings")
        assert not r.remote.payments
    finally:
        await bot.session.close()


async def test_followups_dont_starve_after_one_hundred_completed_orders(rig):
    r = rig
    await enroll(r)
    await r.worker.once()
    worker = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await worker.once()
    # Older completed jobs with receipts must not occupy the bounded follow-up batch.
    j = r.service.job("order")
    for i in range(105):
        ident = "history-" + str(i)
        Orders(r.store).ingest(r.api.add_order(ident))
        with r.store.transaction() as db:
            op = r.store.enqueue("history:" + ident, "gift_send", ident, {}, db)
            db.execute(
                "INSERT INTO gift_jobs(order_id,item_id,operation_id,listing_revision,gift_code,gift_id,reference,state,receipt,completion_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'SUCCEEDED',?,'MANUAL',?,?)",
                (
                    ident,
                    "item",
                    op,
                    j["listing_revision"],
                    "heart",
                    j["gift_id"],
                    "ref-" + ident,
                    j["receipt"],
                    "2025-01-01T00:00:00+00:00",
                    now(),
                ),
            )
            r.service.queue_notice(db, r.service.job(ident), "DELIVERED")
    await enroll(r, ident="new-order")
    # Old test-only placeholders are not dispatch candidates.
    r.store.execute(
        "UPDATE outbox SET status='SUCCEEDED' WHERE operation_key LIKE 'history:%' OR kind='gift_notice'"
    )
    await r.worker.once()
    await worker.once()
    assert r.store.rows("SELECT 1 FROM outbox WHERE operation_key='gift-notice:new-order:DELIVERED'")


@pytest.mark.parametrize("change", ["none", "system", "missing-type", "other-owner", "SENT", "problem"])
async def test_production_refund_admission_contract(rig, change):
    from app.playerok.adapter import Playerok

    r = rig
    order = r.api.add_order()
    order["item"]["seller_type"] = "USER"
    if change == "system":
        order["item"]["seller_type"] = "SYSTEM"
    elif change == "missing-type":
        del order["item"]["seller_type"]
    elif change == "other-owner":
        order["item"]["user"]["id"] = "other"
    elif change == "SENT":
        order["status"] = "SENT"
    elif change == "problem":
        order["has_problem"] = True
    assert await Playerok(r.api.account).gift_refund_capability(order) is (change == "none")


async def test_real_playerok_refund_permit_http_and_independent_readback(rig):
    import json

    import httpx

    from app.playerok.adapter import Playerok, SafeAccount
    from app.playerok.thread_calls import close_calls

    r = rig
    await enroll(r)
    r.api.orders["order"]["item"]["seller_type"] = "USER"
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["operationName"] == "updateDeal"
        assert body["variables"]["input"] == {"id": "order", "status": "ROLLED_BACK"}
        r.api.orders["order"]["status"] = "ROLLED_BACK"
        return httpx.Response(200, json={"data": {"updateDeal": {"id": "order", "status": "ROLLED_BACK"}}})

    account = SafeAccount(
        "token=mock-only", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    account.id = "shop"
    account.automation_enabled = True
    r.api.account = account
    r.api.gift_refund_capability = Playerok(account).gift_refund_capability
    r.remote.resolve_error = errors.UsernameNotOccupiedError(None)
    try:
        await r.worker.once()
        await r.worker.once()
        assert r.service.job("order")["refund_state"] == "CONFIRMED"
        assert len(calls) == 1 and not r.remote.payments
        assert r.store.rows("SELECT status FROM orders WHERE id=?", ("order",))[0]["status"] == "ROLLED_BACK"
    finally:
        await close_calls(r.api)
        account.close()


@pytest.mark.parametrize(
    "error", [errors.FloodWaitError(None, 30), errors.AuthKeyUnregisteredError(None), ConnectionError()]
)
async def test_transient_mtproto_failures_never_become_invalid_username(rig, error):
    r = rig
    await enroll(r)
    r.remote.resolve_error = error
    await r.worker.once()
    assert r.service.job("order")["state"] == "BLOCKED"
    assert r.service.job("order")["refund_state"] == "NONE"
    assert not r.remote.payments and not r.api.refunds


async def test_refunds_need_separate_permission(rig):
    r = rig
    await enroll(r)
    disabled = replace(r.api.gift_settings, refund_enabled=False)
    r.api.gift_settings = disabled
    r.api.support_refund = True
    r.remote.resolve_error = errors.UsernameNotOccupiedError(None)
    await r.worker.once()
    assert r.service.job("order")["refund_state"] == "MANUAL"
    assert not r.api.refunds


async def test_read_only_sender_cannot_invoke_paid_rpc(rig):
    from app.gifts.mtproto import GiftFailure

    r = rig
    r.api.gift_sender.settings = replace(r.api.gift_settings, payment_enabled=False)
    with pytest.raises(GiftFailure, match="GIFT_PAYMENTS_DISABLED"):
        await r.api.gift_sender.pay(None)
    assert not r.remote.payments


async def test_gift_order_navigation_clears_old_secret_input_context(rig):
    r = rig
    await enroll(r)
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await state.set_state("stock:input")
        await state.set_data({"item_id": "unrelated", "secret": "sensitive input"})
        await ui.click("gft:job:order")
        assert await state.get_state() is None and await state.get_data() == {}
        assert "sensitive input" not in session.calls[-1].text
    finally:
        await bot.session.close()


async def test_raw_mtproto_valueerror_is_not_reflected_in_panel(rig):
    r = rig

    async def failed_catalog():
        raise ValueError("do-not-expose-private-peer")

    r.api.gift_sender.catalog = failed_catalog
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    try:
        await ui.click("gft:bind:item")
        assert "do-not-expose-private-peer" not in session.calls[-1].text
        assert "Внутренняя ошибка" in session.calls[-1].text
        assert not r.service.listing("item") and not r.remote.payments
    finally:
        await bot.session.close()
