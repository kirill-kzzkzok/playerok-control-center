"""Isolated, executable final acceptance: UI -> publish -> sync -> real workers.

All Telegram/Playerok transports are in-process doubles. This file never loads a
.env, network client or personal session. Payment calls still pass the production
PersonalGiftSender and durable intent/receipt guards.
"""

import asyncio
import copy
import json
import sqlite3

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Document
from telethon import functions

from app.database.store import Store
from app.gifts.mtproto import PersonalGiftSender
from app.gifts.service import GiftService
from app.gifts.settings import GiftSettings
from app.gifts.worker import GiftWorker
from app.services.account_binding import bind_account
from app.services.listing_launch import ListingLaunch
from app.services.orders import Orders
from app.services.products import Products
from app.telegram.storage import SQLiteStorage
from app.workers.outbox import Executor, OutboxWorker
from app.workers.promotions import PromotionWorker
from app.workers.sync import Synchronizer
from tests.test_gifts import Marketplace, MTProtoTransport
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher

NOTICE = "Все отправлено, спасибо за покупку!"
ORDER = "acceptance-order-heart"


class AcceptancePlayerok(Marketplace):
    """Explicit local Playerok boundary; unexpected methods fail, never forward."""

    def __init__(self):
        super().__init__()
        self.item.update(status="DRAFT", price=100, raw_price=100)
        self.created = []
        self.published = []
        self.completed = []
        self.reads = []
        self.delivery_at_publish = None
        self.store = None
        self.account.create_item = self.create_item
        self.account.publish_item = self.publish_item

    def create_item(self, **kwargs):
        assert not self.created, "The wizard must create exactly one remote DRAFT"
        self.created.append(copy.deepcopy(kwargs))
        self.item.update(
            name=kwargs["name"],
            description=kwargs["description"],
            price=kwargs["price"],
            raw_price=kwargs["price"],
            category={"id": kwargs["game_category_id"]},
            obtaining_type={"id": kwargs["obtaining_type_id"]},
            attachments=[{"id": "acceptance-photo"} for _ in kwargs["attachments"]],
        )
        return copy.deepcopy(self.item)

    def publish_item(self, **kwargs):
        assert len(self.created) == 1 and not self.published
        self.delivery_at_publish = dict(self.store.rows("SELECT * FROM gift_listings")[0])
        assert self.delivery_at_publish["enabled"] == 1
        assert self.delivery_at_publish["armed_at"]
        self.published.append(copy.deepcopy(kwargs))
        self.item["status"] = "APPROVED"
        # Arrival at the first external publication: local enrollment has NOT run.
        # Its only source must be the real production Synchronizer afterwards.
        self.add_order(ORDER, username="@kzzkzok")
        return copy.deepcopy(self.item)

    def update_deal(self, deal_id, new_status):
        assert deal_id in self.orders and new_status.name == "SENT"
        assert (
            self.store.rows("SELECT state FROM gift_jobs WHERE order_id=?", (deal_id,))[0]["state"]
            == "SUCCEEDED"
        )
        assert all(
            g["state"] == "SUCCEEDED" and g["receipt"]
            for g in self.store.rows("SELECT * FROM gift_steps WHERE order_id=?", (deal_id,))
        )
        self.completed.append(deal_id)
        self.orders[deal_id]["status"] = "SENT"
        return copy.deepcopy(self.orders[deal_id])

    def send_message(self, chat_id, text):
        assert text == NOTICE
        order_id = next(o["id"] for o in self.orders.values() if o["chat"]["id"] == chat_id)
        assert (
            self.store.rows("SELECT state FROM gift_jobs WHERE order_id=?", (order_id,))[0]["state"]
            == "SUCCEEDED"
        )
        return super().send_message(chat_id, text)

    async def call(self, method, **kwargs):
        self.reads.append(method)
        if method == "get_games":
            return {
                "games": [{"id": "telegram", "name": "Telegram"}],
                "page_info": {"has_next_page": False, "end_cursor": None},
            }
        if method == "get_game":
            return {"categories": [{"id": "stars", "name": "Stars"}]}
        if method == "get_game_category":
            return {"id": "stars", "name": "Stars", "options": []}
        if method == "get_item_priority_statuses":
            return [{"id": "free", "type": "DEFAULT", "price": 0, "period": 7}]
        if method == "get_deals":
            return {
                "deals": copy.deepcopy(list(self.orders.values())),
                "page_info": {"has_next_page": False, "end_cursor": None},
            }
        if method == "get_chats":
            return {"chats": [], "page_info": {"has_next_page": False, "end_cursor": None}}
        if method == "get_my_items":
            return {
                "items": [copy.deepcopy(self.item)],
                "page_info": {"has_next_page": False, "end_cursor": None},
            }
        return await super().call(method, **kwargs)

    async def pages(self, method, collection, **kwargs):
        if method == "get_game_category_obtaining_types":
            yield [{"id": "gift", "name": "Подарок", "game_category_id": "stars"}]
        elif method == "get_game_category_agreements":
            yield []
        else:
            async for page in super().pages(method, collection, **kwargs):
                yield page


class AcceptanceHarness:
    """One isolated WAL file and replaceable transports; no payment bypass."""

    def __init__(self, path):
        self.path = path
        self.store = Store(path)
        self.api, self.remote = AcceptancePlayerok(), MTProtoTransport()
        self.remote.recipient.username = "kzzkzok"
        self.settings = GiftSettings(
            1,
            "synthetic-api-hash",
            "data/synthetic.session",
            "stars",
            "gift",
            "username-field",
            True,
            15,
            15,
            False,
        )
        self.api.gift_settings = self.settings
        self.api.gift_sender = PersonalGiftSender(self.settings, client=self.remote)
        self.session = TelegramSession()
        self.bot = Bot("123:" + "x" * 35, session=self.session)
        self.attach()

    def attach(self):
        self.api.store = self.store
        self.service = GiftService(self.store, self.api, self.settings, self.api.gift_sender)
        self.sync = Synchronizer(self.store, self.api, Orders(self.store))
        self.worker = OutboxWorker(self.store, Executor(self.store, self.api, Products(self.store, self.api)))
        self.gifts = GiftWorker(self.store, self.api, self.settings, self.api.gift_sender)
        self.ui = Interaction(self.bot, dispatcher(self.store, self.api))
        self.state = FSMContext(SQLiteStorage(self.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
        self.launch = ListingLaunch(self.store, self.api, self.service)

    async def wizard_action(self, action):
        nonce = (await self.state.get_data())["view_token"]
        await self.ui.click(f"w:{nonce}:{action}")

    async def launch_action(self, action):
        nonce = (await self.state.get_data())["launch"]["nonce"]
        await self.ui.click(f"ln:{nonce}:{action}")

    async def create_and_prepare(
        self, *, seed_resume=True, prepare_launch=True, stop_before_delivery=False, description=None
    ):
        await self.ui.message("/start")
        bind_account(self.store, self.api.account.id)
        await self.sync.once()
        assert self.sync.health["status"] == "OK", self.sync.health
        # Explicit test-owner opt-in, never inherited from a production .env.
        if seed_resume:
            self.store.set_setting("gifts:stopped", False)
        await self.ui.click("create")
        for _ in range(3):
            await self.wizard_action("choose:0")
        await self.ui.message("Товар будет передан на указанный Telegram username")
        await self.ui.message("Проверочный Telegram Heart")
        await self.ui.message(description or "Изолированная проверка полного сценария\ngift: heart")
        await self.ui.message(
            document=Document(
                file_id="photo",
                file_unique_id="unique",
                file_name="heart.png",
                mime_type="image/png",
                file_size=len(self.session.image),
            )
        )
        await self.wizard_action("photos_done")
        await self.ui.message("100")
        if stop_before_delivery:
            return
        await self.wizard_action("choose:3")
        await self.wizard_action("confirm_done")
        self.original_draft = (await self.state.get_data())["draft_id"]
        await self.wizard_action("submit")
        await self.worker.once()
        assert len(self.api.created) == 1
        assert self.api.item["status"] == "DRAFT"
        assert self.store.get_draft(self.original_draft, 7)["state"] == "REMOTE_DRAFT"
        if not prepare_launch:
            return
        await self.ui.click("launch:from:" + self.original_draft)
        await self.launch_action("edit:after")
        await self.ui.message(NOTICE)
        # New launch defaults include supported completion. Preserve an explicit
        # prior opt-out instead of toggling the new default off in this fixture.
        launch_view = (await self.state.get_data())["launch"]
        if not self.store.get_draft(launch_view["id"], 7)["payload"]["auto_complete"]:
            await self.launch_action("complete")
        await self.launch_action("check")
        self.launch_id = (await self.state.get_data())["launch"]["id"]
        assert self.store.get_draft(self.launch_id, 7)["payload"].get("ready"), self.session.calls[-1].text

    def restart(self):
        self.store.close()
        self.store = Store(self.path)
        self.attach()

    async def close(self):
        await self.bot.session.close()
        await self.api.gift_sender.close()
        self.store.close()


@pytest.fixture
async def acceptance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    harness = AcceptanceHarness(tmp_path / "isolated-acceptance.sqlite")
    try:
        yield harness
    finally:
        await harness.close()


async def test_complete_heart_sale_from_dispatcher_through_receipt_and_restart(acceptance):
    h = acceptance
    await h.create_and_prepare()
    h.restart()  # Restores the durable final preview, not a hand-built perfect DB.
    await h.ui.click("launch:open:" + h.launch_id)
    await h.launch_action("commit")
    operation = h.store.rows("SELECT * FROM outbox WHERE kind='publish'")
    assert len(operation) == 1
    payload = json.loads(operation[0]["payload"])
    marker = h.store.setting("launch:prepared:item")
    assert marker["operation"] == operation[0]["id"]
    assert payload["delivery_revision"] == marker["delivery_revision"]
    assert h.service.listing("item")["enabled"]
    assert not h.remote.payments and not h.api.published
    assert not h.store.rows("SELECT * FROM orders")
    assert await h.worker.once()
    assert len(h.api.published) == 1
    assert h.api.item["status"] == "APPROVED"
    assert not h.store.rows("SELECT * FROM orders")  # Only the real synchronizer may ingest.
    await h.sync.once()
    assert len(h.store.rows("SELECT * FROM orders")) == 1
    assert h.sync.health["status"] == "OK"
    assert await h.gifts.once() == 1
    assert not h.api.sent and not h.remote.payments
    assert len(h.store.rows("SELECT * FROM gift_jobs")) == 1
    assert len(h.store.rows("SELECT * FROM gift_steps")) == 1
    assert len(h.store.rows("SELECT * FROM outbox WHERE kind='gift_send'")) == 1
    await h.sync.once()
    assert await h.gifts.once() == 0
    assert await h.worker.once()
    assert len(h.remote.payments) == 1 and h.remote.stars == 185
    job, step = h.service.job(ORDER), h.service.steps(ORDER)[0]
    assert job["state"] == step["state"] == "SUCCEEDED"
    receipt = json.loads(step["receipt"])
    assert receipt["recipient_id"] == 55
    assert receipt["gift_id"] == str(h.remote.gift.id)
    assert receipt["reference"] == step["reference"]
    assert receipt["message_id"] == step["message_id"] == 1
    assert step["payer_id"] == 99 and step["spent_at"] and step["form_id"]
    assert any(isinstance(c, functions.contacts.ResolveUsernameRequest) for c in h.remote.calls)
    assert not h.api.sent and not h.api.completed
    assert await h.gifts.once() == 0
    while await h.worker.once():
        pass
    assert h.api.sent == [("chat-" + ORDER, NOTICE)]
    assert h.api.completed == [ORDER]
    assert h.api.orders[ORDER]["status"] == "SENT"
    completion = h.store.rows(
        "SELECT status FROM outbox WHERE id=?", (h.service.job(ORDER)["completion_operation_id"],)
    )[0]
    assert completion["status"] == "SUCCEEDED"
    await PromotionWorker(h.store, h.api).once()  # Old/default dry-run has no spend authority.
    h.restart()
    for _ in range(3):
        await h.sync.once()
        await h.gifts.once()
        while await h.worker.once():
            pass
    assert len(h.api.created) == len(h.api.published) == len(h.remote.payments) == 1
    assert len(h.api.sent) == len(h.api.completed) == 1
    assert len(h.store.rows("SELECT * FROM gift_jobs")) == len(h.store.rows("SELECT * FROM gift_steps")) == 1
    assert not h.store.rows("SELECT * FROM outbox WHERE status IN ('PENDING','PROCESSING','UNCERTAIN')")
    assert not h.store.rows(
        "SELECT * FROM outbox WHERE kind IN ('premium_publish','premium_bump','gift_refund')"
    )


async def published_sale(h):
    await h.create_and_prepare()
    await h.launch_action("commit")
    await h.worker.once()
    await h.sync.once()
    assert await h.gifts.once() == 1
    assert not h.remote.payments and not h.api.sent


@pytest.mark.parametrize(
    "fault",
    [
        "unpaid",
        "cancelled",
        "missing_username",
        "wrong_field",
        "username_moved",
        "gift_missing",
        "price_changed",
        "low_balance",
        "recipient_forbidden",
        "description_changed",
        "owner_stop",
        "resolve_timeout",
    ],
)
async def test_real_pipeline_preflight_faults_never_spend_or_announce_success(acceptance, fault):
    h = acceptance
    await published_sale(h)
    order = h.api.orders[ORDER]
    if fault == "unpaid":
        order["status"] = "CREATED"
    elif fault == "cancelled":
        order["status"] = "ROLLED_BACK"
    elif fault == "missing_username":
        order["obtaining_fields"] = []
        order["comment_from_buyer"] = "@kzzkzok"
    elif fault == "wrong_field":
        order["obtaining_fields"][0]["id"] = "untrusted-same-label"
    elif fault == "username_moved":
        h.remote.changed_user = True
    elif fault == "gift_missing":
        h.remote.gift = None
    elif fault == "price_changed":
        h.remote.gift.stars += 1
    elif fault == "low_balance":
        h.remote.stars = 14
    elif fault == "recipient_forbidden":
        h.remote.disallow_gifts = True
    elif fault == "description_changed":
        h.api.item["description"] = "Описание изменено\ngift: rose"
    elif fault == "owner_stop":
        h.service.stop(7)
    elif fault == "resolve_timeout":
        h.remote.resolve_error = TimeoutError()
    await h.worker.once()
    await h.gifts.once()
    assert not h.remote.payments and not h.api.sent and not h.api.completed
    assert not h.api.refunds
    assert not h.store.rows(
        "SELECT * FROM outbox WHERE kind='gift_notice' AND json_extract(payload,'$.outcome')='DELIVERED'"
    )
    assert h.service.job(ORDER)["state"] != "SUCCEEDED"
    assert not h.service.steps(ORDER)[0]["spent_at"]
    h.restart()
    await h.sync.once()
    await h.gifts.once()
    # Never run a queued INVALID notice through a fake that only allows DELIVERED.
    assert not h.store.rows(
        "SELECT * FROM outbox WHERE kind='gift_send' AND status IN ('PENDING','PROCESSING')"
    )
    assert not h.remote.payments


@pytest.mark.parametrize(
    "fault", ["lost_ack", "history_absent", "wrong_recipient", "wrong_gift", "unexpected_comment", "refunded"]
)
async def test_sent_rpc_without_independent_matching_receipt_blocks_replay(acceptance, fault):
    h = acceptance
    await published_sale(h)
    original = h.remote.get_messages
    if fault == "lost_ack":
        h.remote.pay_error = TimeoutError()
    else:

        async def read(peer, **kwargs):
            messages = copy.deepcopy(await original(peer, **kwargs))
            if fault == "history_absent":
                return []
            for m in messages:
                if fault == "wrong_recipient":
                    m.peer_id.user_id = 77
                elif fault == "wrong_gift":
                    m.action.gift.id += 1
                elif fault == "unexpected_comment":
                    from telethon import types

                    m.action.message = types.TextWithEntities("unexpected public text", [])
                elif fault == "refunded":
                    m.action.refunded = True
            return messages

        h.remote.get_messages = read
    await h.worker.once()
    assert len(h.remote.payments) == 1
    job, step = h.service.job(ORDER), h.service.steps(ORDER)[0]
    assert job["state"] == step["state"] == "UNCERTAIN"
    assert step["spent_at"] and step["form_id"] and not step["receipt"]
    assert not h.api.sent and not h.api.completed and not h.api.refunds
    assert not h.store.rows("SELECT * FROM outbox WHERE kind IN ('gift_notice','fulfill','gift_refund')")
    h.restart()
    for _ in range(2):
        await h.sync.once()
        await h.gifts.once()
        await h.worker.once()
    assert len(h.remote.payments) == 1
    assert h.service.job(ORDER)["state"] == "UNCERTAIN"
    assert not h.api.sent and not h.api.completed and not h.api.refunds


async def test_cancellation_immediately_after_durable_intent_never_retries_rpc(acceptance, monkeypatch):
    h = acceptance
    await published_sale(h)

    async def lost_process(prepared):
        step = h.service.steps(ORDER)[0]
        assert step["state"] == "SPENDING" and step["spent_at"] and step["form_id"]
        raise asyncio.CancelledError

    monkeypatch.setattr(h.api.gift_sender, "pay", lost_process)
    with pytest.raises(asyncio.CancelledError):
        await h.worker.once()
    assert not h.remote.payments
    assert h.service.steps(ORDER)[0]["state"] == "UNCERTAIN"
    h.restart()
    await h.sync.once()
    await h.gifts.once()
    assert await h.worker.once() is False
    assert not h.api.sent and not h.api.completed and not h.remote.payments


async def test_receipt_read_then_disk_failure_recovery_does_not_buy_again(acceptance, monkeypatch):
    h = acceptance
    await published_sale(h)
    original = h.store.finish

    def disk_failure(ident, token, status, *args, **kwargs):
        if status == "SUCCEEDED":
            raise sqlite3.OperationalError("injected receipt commit failure")
        return original(ident, token, status, *args, **kwargs)

    monkeypatch.setattr(h.store, "finish", disk_failure)
    with pytest.raises(sqlite3.OperationalError):
        await h.worker.once()
    assert len(h.remote.payments) == 1
    assert h.service.steps(ORDER)[0]["state"] == "SPENDING"
    h.store.execute("UPDATE outbox SET lease_until=0 WHERE kind='gift_send'")
    h.restart()
    assert h.store.recover() == 1
    assert h.service.job(ORDER)["state"] == "UNCERTAIN"
    assert await h.worker.once() is False
    await h.service.reconcile(7, ORDER)
    assert h.service.job(ORDER)["state"] == "SUCCEEDED"
    await h.gifts.once()
    while await h.worker.once():
        pass
    assert len(h.remote.payments) == 1
    assert h.api.sent == [("chat-" + ORDER, NOTICE)] and h.api.completed == [ORDER]


async def test_duplicate_sync_and_parallel_orders_cannot_exceed_fifteen_star_authority(acceptance):
    h = acceptance
    await published_sale(h)
    h.api.add_order("acceptance-second-order", "@kzzkzok")
    await h.sync.once()
    assert await h.gifts.once() == 1
    await h.sync.once()
    assert await h.gifts.once() == 0
    assert len(h.store.rows("SELECT * FROM gift_jobs")) == 2
    await asyncio.gather(h.worker.once(), h.worker.once())
    assert len(h.remote.payments) == 1 and h.remote.stars == 185
    states = {r["state"] for r in h.store.rows("SELECT state FROM gift_jobs")}
    assert states == {"SUCCEEDED", "BLOCKED"}
    assert (
        h.store.rows("SELECT SUM(stars) AS stars FROM gift_steps WHERE spent_at IS NOT NULL")[0]["stars"]
        == 15
    )
    assert not h.api.refunds
    h.restart()
    await h.sync.once()
    await h.gifts.once()
    # Follow-ups are allowed only for the one verified delivery.
    while await h.worker.once():
        pass
    assert len(h.remote.payments) == 1 and len(h.api.sent) == len(h.api.completed) == 1


def last_buttons(h):
    markup = h.session.calls[-1].reply_markup
    return [button for row in markup.inline_keyboard for button in row]


async def test_fresh_owner_can_explicitly_resume_then_launch_first_gift_draft(acceptance):
    h = acceptance
    await h.ui.message("/start")
    await h.sync.once()
    assert h.store.setting("gifts:stopped", True) is True
    assert not h.store.rows("SELECT * FROM gift_listings")
    await h.ui.click("gft:settings")
    assert any(b.callback_data == "gft:resume" for b in last_buttons(h))
    await h.ui.click("gft:resume")
    confirm = next(
        b.callback_data for b in last_buttons(h) if (b.callback_data or "").startswith("gft:resume_confirm:")
    )
    assert h.store.setting("gifts:stopped", True) is True
    await h.ui.click(confirm)
    assert h.store.setting("gifts:stopped", True) is False
    assert not h.store.rows("SELECT * FROM gift_listings")
    assert not h.remote.payments and not h.store.rows("SELECT * FROM outbox")
    await h.ui.click(confirm)
    assert not h.remote.payments and not h.store.rows("SELECT * FROM outbox")
    await h.create_and_prepare(seed_resume=False)
    assert h.store.get_draft(h.launch_id, 7)["payload"]["ready"]
    assert not h.remote.payments


async def test_resume_confirmation_cannot_cancel_newer_stop_during_network_check(acceptance, monkeypatch):
    h = acceptance
    await h.ui.message("/start")
    await h.sync.once()
    await h.ui.click("gft:resume")
    confirm = next(
        b.callback_data for b in last_buttons(h) if (b.callback_data or "").startswith("gft:resume_confirm:")
    )
    original = h.api.gift_sender.catalog

    async def stop_during_refresh():
        h.service.stop(7)
        return await original()

    monkeypatch.setattr(h.api.gift_sender, "catalog", stop_during_refresh)
    await h.ui.click(confirm)
    assert h.store.setting("gifts:stopped", True) is True
    assert not h.remote.payments and not h.store.rows("SELECT * FROM outbox")


async def test_known_session_failure_is_explained_without_raw_machine_code(acceptance, monkeypatch):
    from app.gifts.mtproto import GiftFailure

    h = acceptance
    await h.ui.message("/start")

    async def unauthorized():
        raise GiftFailure("SESSION_UNAUTHORIZED")

    monkeypatch.setattr(h.api.gift_sender, "catalog", unauthorized)
    await h.ui.click("gft:refresh")
    text = h.session.calls[-1].text
    assert "SESSION_UNAUTHORIZED" not in text
    assert "авториз" in text.lower() and "личн" in text.lower()
    assert not h.remote.payments
