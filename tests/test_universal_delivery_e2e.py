"""UI → registry → catalog relist → sync → existing workers; API boundaries only."""

import copy
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store, now
from app.gifts.mtproto import PersonalGiftSender
from app.gifts.settings import GiftSettings
from app.gifts.worker import GiftWorker
from app.services.inventory import Inventory
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.services.products import Products
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.logical_products import LogicalProductWorker
from app.workers.outbox import Executor, OutboxWorker
from app.workers.sync import Synchronizer
from tests.test_gift_bundles import BundleTransport
from tests.test_logical_products import ReadAPI
from tests.test_telegram_integration import Interaction, TelegramSession
from tests.test_telegram_transport import production_dispatcher
from tests.test_ux_redesign import buttons


class MarketplaceBoundary(ReadAPI):
    def __init__(self, gift=False):
        super().__init__()
        self.item["user"]["role"] = "USER"
        if gift:
            self.item.update(category={"id": "stars"}, obtaining_type={"id": "gift"})
        self.items = {"lot": self.item}
        self.orders, self.messages = {}, []
        self.account.send_message = self.send_message
        self.account.update_deal = self.update_deal
        self.gift_settings = GiftSettings(
            1, "test-hash", "data/test.session", "stars", "gift", "username-field", True, 100, 300
        )
        self.remote = BundleTransport()
        self.gift_sender = PersonalGiftSender(self.gift_settings, client=self.remote)

    @staticmethod
    def page(key, values):
        return {key: copy.deepcopy(values), "page_info": {"has_next_page": False}}

    async def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == "get_item":
            return copy.deepcopy(self.items[kwargs["id"]])
        if method == "get_my_items":
            return self.page("items", [item for item in self.items.values() if item["status"] == "APPROVED"])
        if method == "get_deals":
            return self.page("deals", list(self.orders.values()))
        if method in {"get_deal", "get_delivery_deal"}:
            return copy.deepcopy(self.orders[kwargs["deal_id"]])
        if method == "get_chats":
            return self.page("chats", [{"id": o["chat"]["id"]} for o in self.orders.values()])
        if method == "get_chat":
            return {"id": kwargs["chat_id"], "is_texting_allowed": True}
        if method == "get_chat_messages":
            return self.page("messages", [m for m in self.messages if m["chat_id"] == kwargs["chat_id"]])
        raise AssertionError(method)

    async def pages(self, method, collection, **kwargs):
        assert method == "get_game_category_data_fields"
        yield [{"id": "username-field", "type": "OBTAINING_DATA", "required": True}]

    def send_message(self, chat_id, text):
        message = {
            "id": "message-" + str(len(self.messages) + 1),
            "chat_id": chat_id,
            "text": text,
            "user": {"id": "seller"},
            "created_at": now(),
        }
        self.messages.append(message)
        return copy.deepcopy(message)

    def update_deal(self, deal_id, new_status):
        self.orders[deal_id]["status"] = new_status.name
        return copy.deepcopy(self.orders[deal_id])

    def order(self, ident, item_id="lot", status="PAID"):
        order = {
            "id": ident,
            "status": status,
            "direction": "OUT",
            "has_problem": False,
            "is_automated": False,
            "created_at": now(),
            "item": copy.deepcopy(self.items[item_id]),
            "chat": {"id": "chat-" + ident},
            "user": {"id": "buyer-" + ident},
            "obtaining_fields": [{"id": "username-field", "value": "@Realbuyer"}],
        }
        self.orders[ident] = order
        return order


@pytest.fixture
async def simulation(tmp_path):
    resources = []

    async def create(gift=False):
        store = Store(tmp_path / ("gifts.sqlite3" if gift else "stock.sqlite3"))
        store.claim_first_owner(7)
        store.set_setting("playerok:account_id", "seller")
        store.set_setting("gifts:stopped", False)
        store.set_setting("orders:last_complete_sync", now())
        api, session = MarketplaceBoundary(gift), TelegramSession()
        bot = Bot("123:" + "x" * 35, session=session)
        ui = Interaction(bot, production_dispatcher(store, api))
        r = SimpleNamespace(store=store, api=api, bot=bot, session=session, ui=ui)
        r.worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
        resources.append(r)
        await ui.message("/start")
        return r

    yield create
    for r in resources:
        await r.bot.session.close()
        r.store.close()


async def click(r, *, action=None, suffix=None):
    visible = buttons(r.session.last_panel)
    key = action or next(k for k in visible if k.endswith(suffix))
    assert key in visible, (key, visible)
    await r.ui.click(key)
    text = getattr(r.session.last_panel, "text", "") or ""
    assert not text.startswith("❗"), text
    return key


async def setup_ui(r, handler, value=None, *, completion=False):
    await click(r, action="items")
    await click(r, action="item:lot")
    await click(r, action="iv:delivery:lot")
    await click(r, action="lp:method:lot")
    await click(r, suffix=":mode:" + handler)
    if handler in {"fixed_text", "telegram_gifts"}:
        await r.ui.message(value)
    if handler == "unique_stock":
        await click(r, suffix=":stockinput")
        await r.ui.message("isolated-unit-one\nisolated-unit-two\nisolated-unit-three")
        await click(r, suffix=":stock_commit")
    await click(r, suffix=":input:after")
    await r.ui.message("Спасибо за покупку!")
    context = FSMContext(r.ui.dp.storage, StorageKey(bot_id=r.bot.id, chat_id=7, user_id=7))
    config = (await context.get_data())["logical_setup"]["config"]
    selected = (
        config.get("auto_fulfill", False)
        if handler == "telegram_gifts"
        else config.get("automatic_completion", False)
    )
    if bool(selected) != completion:
        await click(r, suffix=":completion")
    await click(r, suffix=":preview")
    assert not r.store.rows("SELECT * FROM outbox")
    # Dispatcher/FSM is recreated, as on restart. The durable preview survives.
    r.ui.dp = production_dispatcher(r.store, r.api)
    commit = await click(r, suffix=":commit")
    await r.ui.click(commit)  # Old nonce cannot add a second operation.
    assert len(r.store.rows("SELECT * FROM outbox")) == 1
    await r.worker.once()
    # The rejected old confirmation offers the normal menu recovery path.
    await click(r, action="home")
    await click(r, action="items")
    await click(r, action="item:lot")
    await click(r, action="iv:delivery:lot")
    await click(r, suffix=":arm")
    await click(r, suffix=":enable:inherit")
    assert r.store.rows("SELECT enabled FROM logical_products")[0]["enabled"] == 1
    assert not r.api.messages and not r.api.remote.payments


@pytest.mark.parametrize(
    "handler,value",
    [
        ("fixed_text", "one-confirmed-text"),
        ("unique_stock", None),
        ("telegram_gifts", "gift: heart"),
        ("telegram_gifts", "gift: heart, heart, bear"),
    ],
)
async def test_ui_binding_relist_paid_delivery_and_thanks(simulation, handler, value):
    r = await simulation(gift=handler == "telegram_gifts")
    await setup_ui(r, handler, value)
    product = LogicalProducts(r.store, r.api).listing("lot")
    r.api.item["status"] = "SOLD"
    r.api.items["lot2"] = {
        **copy.deepcopy(r.api.item),
        "id": "lot2",
        "status": "APPROVED",
        "price": 90,
        "discount": 10,
        "attachments": [{"id": "updated-photo"}],
    }
    await LogicalProductWorker(r.store, r.api).once()
    assert LogicalProducts(r.store, r.api).listing("lot2")["id"] == product["id"]
    r.api.order("paid", "lot2", status="PENDING")
    synchronizer = Synchronizer(r.store, r.api, Orders(r.store))
    await synchronizer.once()
    assert not r.store.rows("SELECT * FROM logical_order_scopes")
    r.api.orders["paid"]["status"] = "PAID"
    await synchronizer.once()
    gifts = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    for _ in range(12):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await gifts.once()
        await r.worker.once()
    scopes = r.store.rows("SELECT * FROM logical_order_scopes")
    assert len(scopes) == 1 and scopes[0]["product_id"] == product["id"]
    sent = [m["text"] for m in r.api.messages]
    assert sent.count("Спасибо за покупку!") == 1
    if handler == "telegram_gifts":
        expected = ["heart"] if value == "gift: heart" else ["heart", "heart", "bear"]
        steps = r.store.rows("SELECT * FROM gift_steps ORDER BY position")
        assert [s["code"] for s in steps] == expected
        assert all(s["state"] == "SUCCEEDED" and s["message_id"] for s in steps)
        assert len(r.api.remote.payments) == len(expected)
    else:
        assert len(sent) == 2
        assert r.store.rows("SELECT status FROM delivery_chains")[0]["status"] == "SUCCEEDED"
        if handler == "unique_stock":
            assert Inventory(r.store, r.api).counts("lot2")["AVAILABLE"] == 2
            assert Inventory(r.store, r.api).counts("lot2")["DELIVERED"] == 1
        else:
            assert sent[0] == value


async def test_fast_paid_relist_is_discovered_even_after_it_is_already_sold(simulation):
    r = await simulation()
    await setup_ui(r, "fixed_text", "verified-text")
    r.api.item["status"] = "SOLD"
    r.api.items["lot2"] = {**copy.deepcopy(r.api.item), "id": "lot2", "status": "SOLD"}
    r.api.order("fast-paid", "lot2")
    await Synchronizer(r.store, r.api, Orders(r.store)).once()
    for _ in range(3):
        await LogicalProductWorker(r.store, r.api).once()
        await EnrollmentWorker(r.store, r.api).once()
    assert LogicalProducts(r.store, r.api).listing("lot2") is not None
    assert len(r.store.rows("SELECT * FROM delivery_chains")) == 1
    for _ in range(5):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert [m["text"] for m in r.api.messages] == ["verified-text", "Спасибо за покупку!"]


async def test_duplicate_marker_has_owner_notification_and_no_inherited_delivery(simulation):
    from app.workers.notifications import Notifications

    r = await simulation()
    await setup_ui(r, "fixed_text", "verified-text")
    r.store.set_setting("notifications:initialized", True)
    r.api.items["duplicate"] = {**copy.deepcopy(r.api.item), "id": "duplicate"}
    assert await LogicalProductWorker(r.store, r.api).once() == 0
    assert LogicalProducts(r.store, r.api).listing("duplicate") is None
    await Notifications(r.store, r.bot).once()
    assert (
        r.store.rows("SELECT status FROM notifications WHERE event_key LIKE 'logical-problem:%'")[0]["status"]
        == "SUCCEEDED"
    )
    assert not r.api.messages
