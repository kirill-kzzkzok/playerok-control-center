"""Exercise aiogram's real dispatcher, FSM, routers and middleware without network."""

from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import CallbackQuery, Chat, Document, File, Message, Update, User
from PIL import Image

from app.database.store import Store
from app.security.access import OwnerMiddleware
from app.services.attachments import Attachments
from app.services.products import Products
from app.telegram.router import create_router
from app.telegram.storage import SQLiteStorage


class TelegramSession(BaseSession):
    @property
    def last_panel(self):
        return next(
            method
            for method in reversed(self.calls)
            if method.__api_method__
            in {"sendMessage", "editMessageText", "sendPhoto", "editMessageMedia", "editMessageCaption"}
        )

    def __init__(self):
        super().__init__()
        self.calls = []
        stream = BytesIO()
        Image.new("RGB", (40, 40), "purple").save(stream, "PNG")
        self.image = stream.getvalue()

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = method.__api_method__
        if name == "getMe":
            return User(id=123, is_bot=True, first_name="Test", username="pcc_test_bot")
        if name == "getFile":
            return File(
                file_id="photo",
                file_unique_id="unique",
                file_path="images/photo.png",
                file_size=len(self.image),
            )
        if name in {"editMessageReplyMarkup", "deleteMessage"}:
            return True
        if name in {"sendMessage", "editMessageText", "sendPhoto", "editMessageMedia", "editMessageCaption"}:
            return Message(
                message_id=getattr(method, "message_id", None) or len(self.calls),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=getattr(method, "text", None),
                caption=getattr(method, "caption", None),
            )
        if name == "answerCallbackQuery":
            return True
        raise AssertionError(name)

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield self.image


class CatalogAPI:
    account = SimpleNamespace(id="seller", writes_enabled=False)

    def __init__(self):
        self.calls = []

    async def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == "get_games":
            return {"games": [{"id": "game", "name": "Game"}], "page_info": {}}
        if method == "get_game":
            return {"categories": [{"id": "category", "name": "Category"}]}
        if method == "get_game_category":
            return {"id": kwargs["id"], "options": []}
        raise AssertionError(method)

    async def pages(self, method, collection, **kwargs):
        self.calls.append((method, kwargs))
        yield []


def dispatcher(store, api):
    dp = Dispatcher(storage=SQLiteStorage(store), events_isolation=SimpleEventIsolation())
    access = OwnerMiddleware([], store, first_start_claim=True)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(create_router(store, api, Products(store, api), SimpleNamespace(once=None)))
    return dp


class Interaction:
    def __init__(self, bot, dp):
        self.bot, self.dp, self.counter = bot, dp, 0

    async def message(self, text=None, owner=7, document=None, group=False, caption=None):
        self.counter += 1
        entities = [{"type": "bot_command", "offset": 0, "length": 6}] if text == "/start" else None
        message = Message(
            message_id=self.counter,
            date=datetime.now(UTC),
            chat=Chat(id=owner, type="group" if group else "private"),
            from_user=User(id=owner, first_name="Owner", is_bot=False),
            text=text,
            entities=entities,
            document=document,
            caption=caption,
        )
        return await self.dp.feed_update(self.bot, Update(update_id=self.counter, message=message))

    async def click(self, action, owner=7, *, message_id=None, reply_markup=None):
        self.counter += 1
        panel = self.dp.storage.store.setting(f"navigation:{self.bot.id}:{owner}", {})
        message = Message(
            message_id=message_id or panel.get("id") or 1,
            date=datetime.now(UTC),
            chat=Chat(id=owner, type="private"),
            text="Menu",
            reply_markup=reply_markup,
            from_user=User(id=self.bot.id, first_name="Bot", is_bot=True),
        )
        query = CallbackQuery(
            id=str(self.counter),
            from_user=User(id=owner, first_name="Owner", is_bot=False),
            chat_instance="test",
            message=message,
            data=action,
        )
        return await self.dp.feed_update(self.bot, Update(update_id=self.counter, callback_query=query))


async def test_dispatcher_full_draft_with_upload_restart_and_double_submit(tmp_path, monkeypatch):
    store = Store(tmp_path / "telegram.sqlite")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    api = CatalogAPI()
    uploads = Attachments(tmp_path / "uploads")
    monkeypatch.setattr("app.telegram.wizard.Attachments", lambda: uploads)
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.message("/start")
        await ui.click("create")
        for _ in range(2):
            nonce = (await state.get_data())["view_token"]
            await ui.click(f"w:{nonce}:choose:0")
        assert await state.get_state() == "wizard"
        await ui.message("Private buyer delivery data")
        await ui.message("Test product")
        await ui.message("<description>")
        await ui.message(
            document=Document(
                file_id="photo",
                file_unique_id="unique",
                file_name="image.png",
                mime_type="image/png",
                file_size=len(session.image),
            )
        )
        ident = (await state.get_data())["draft_id"]
        assert len(store.get_draft(ident, 7)["payload"]["photos"]) == 1
        # Fresh dispatcher restores the same SQLite FSM and owner.
        ui.dp = dispatcher(store, api)
        nonce = (await state.get_data())["view_token"]
        await ui.click(f"w:{nonce}:photos_done")
        await ui.message("100")
        nonce = (await state.get_data())["view_token"]
        await ui.click(f"w:{nonce}:choose:0")
        nonce = (await state.get_data())["view_token"]
        await ui.click(f"w:{nonce}:confirm_done")
        assert "&lt;description&gt;" in session.calls[-1].text
        nonce = (await state.get_data())["view_token"]
        await ui.click(f"w:{nonce}:submit")
        await ui.click(f"w:{nonce}:submit")
        assert not store.rows("SELECT * FROM outbox")  # read-only config truly blocks network writes
        assert store.get_draft(ident, 7)["state"] == "DRAFT"
        assert "отключены" in session.last_panel.text
    finally:
        await bot.session.close()
        store.close()


async def test_dispatcher_denies_foreign_callbacks_and_group_registration(tmp_path):
    store = Store(tmp_path / "access.sqlite")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    api = CatalogAPI()
    ui = Interaction(bot, dispatcher(store, api))
    try:
        await ui.message("/start", owner=8, group=True)
        assert not store.rows("SELECT * FROM administrators")
        await ui.message("/start", owner=7)
        api.calls.clear()
        await ui.click("create", owner=8)
        await ui.click("commit:forged", owner=8)
        assert not api.calls
        assert not store.rows("SELECT * FROM drafts")
        assert not store.rows("SELECT * FROM outbox")
        assert session.calls[-1].text == "Доступ закрыт"
    finally:
        await bot.session.close()
        store.close()


async def test_financial_panel_through_real_dispatcher_is_read_only(tmp_path):
    from tests.test_finance_panel import FinancialAPI

    store = Store(tmp_path / "finance-dispatcher.sqlite")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    api = FinancialAPI()
    ui = Interaction(bot, dispatcher(store, api))
    try:
        await ui.message("/start")
        await ui.click("settings")
        await ui.click("providers")
        await ui.click("fin:select:SBP")
        assert "сохранён" in session.calls[-1].text
        await ui.click("finance:item")
        assert "На руки после вывода: 1692.00 ₽" in session.calls[-1].text
        await ui.click("fin:cost:item")
        await ui.message("500")
        assert store.setting("finance:7:cost:item") == "500.00"
        count = len(api.calls)
        await ui.click("finance:item", owner=8)
        assert len(api.calls) == count
        assert session.calls[-1].text == "Доступ закрыт"
        assert not store.rows("SELECT * FROM outbox")
        assert not store.rows("SELECT * FROM financial_operations")
        assert store.rows("SELECT count(*) AS n FROM financial_quotes")[0]["n"] >= 2
    finally:
        await bot.session.close()
        store.close()


async def test_notification_reply_keeps_original_and_updates_after_durable_completion(tmp_path):
    from app.services.orders import Orders
    from app.workers.notifications import Notifications
    from app.workers.outbox import Executor, OutboxWorker
    from tests.stateful_telegram import StatefulTelegram
    from tests.test_logical_products import ReadAPI

    api = ReadAPI()
    sent = []

    async def boundary(method, **kwargs):
        if method == "get_chat_messages":
            return {"messages": [], "page_info": {"has_next_page": False}}
        assert method == "get_chat"
        return {"id": kwargs["chat_id"], "is_texting_allowed": True}

    def send_message(chat_id, text):
        sent.append((chat_id, text))
        return {"id": "remote-answer"}

    api.call, api.account.send_message = boundary, send_message
    store = Store(tmp_path / "notification-reply.sqlite")
    store.set_setting("playerok:account_id", "seller")
    session = StatefulTelegram()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.message("/start")
        old = await bot.send_message(7, "Original question")
        Orders(store).ingest_message("chat", {"id": "incoming", "text": "Original question"})
        store.execute(
            "INSERT INTO notifications(event_key,owner_id,telegram_message_id,status) VALUES('message:incoming',7,?,'SUCCEEDED')",
            (old.message_id,),
        )
        store.set_setting("notifications:initialized", True)
        await ui.click("reply:chat", message_id=old.message_id)
        assert await state.get_state() == "chatcard:reply"
        ui.dp = dispatcher(store, api)
        await ui.message("Answer <buyer>")
        assert len(store.rows("SELECT * FROM outbox")) == 1 and not sent
        worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
        await worker.once()
        assert not await worker.once()
        await Notifications(store, bot).once()
        card = store.rows("SELECT * FROM chat_cards")[0]
        shown = session.messages[card["telegram_message_id"]]["text"]
        assert "Original question" in shown and "Answer &lt;buyer&gt;" in shown
        assert "подтверждён Playerok" in shown
        assert session.messages[old.message_id]["exists"]
        assert sent == [("chat", "Answer <buyer>")]
    finally:
        await bot.session.close()
        store.close()


async def test_text_reply_card_rejects_document_and_preserves_context_after_restart(tmp_path):
    from tests.stateful_telegram import StatefulTelegram
    from tests.test_logical_products import ReadAPI

    api = ReadAPI()

    async def boundary(method, **kwargs):
        if method == "get_chat_messages":
            return {"messages": [], "page_info": {"has_next_page": False}}
        assert method == "get_chat"
        return {"id": kwargs["chat_id"], "is_texting_allowed": True}

    api.call = boundary
    store = Store(tmp_path / "photo-ui.sqlite")
    store.set_setting("playerok:account_id", "seller")
    session = StatefulTelegram()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.message("/start")
        await ui.click("reply:chat")
        await ui.message(
            document=Document(
                file_id="photo", file_unique_id="photo-u", file_name="image.png", mime_type="image/png"
            ),
            caption="<photo caption>",
        )
        assert await state.get_state() == "chatcard:reply"
        assert not store.rows("SELECT * FROM outbox")
        assert not store.rows("SELECT * FROM chat_uploads")
        ui.dp = dispatcher(store, api)
        await ui.message("Text reply after restart")
        assert len(store.rows("SELECT * FROM outbox WHERE kind='reply'")) == 1
        assert not store.rows("SELECT * FROM chat_uploads")
        assert await state.get_state() is None
    finally:
        await bot.session.close()
        store.close()


async def test_uncertain_resolution_ui_survives_restart_without_enabling_writes(tmp_path):
    from app.database.store import now

    class EvidenceAPI(CatalogAPI):
        async def call(self, method, **kwargs):
            assert method == "get_chat_messages"
            self.calls.append((method, kwargs))
            return {
                "messages": [
                    {"id": "verified", "text": "answer", "user": {"id": "seller"}, "created_at": now()}
                ],
                "page_info": {"has_next_page": False},
            }

    store = Store(tmp_path / "resolution-ui.sqlite")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    api = EvidenceAPI()
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.message("/start")
        ident = store.enqueue("uncertain", "reply", "chat", {"text": "answer"})
        action = store.claim()
        store.finish(ident, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
        await ui.click("ops:list:UNCERTAIN:0")
        await ui.click("ops:show:" + ident)
        await ui.click("ops:confirm:" + ident)
        assert await state.get_state() == "resolve:message"
        await ui.message("verified")
        await ui.message("Лично проверил ответ в нужном диалоге")
        assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
        token = (await state.get_data())["resolution"]["nonce"]
        ui.dp = dispatcher(store, api)
        await ui.click("ops:apply:" + token)
        await ui.click("ops:apply:" + token)
        assert store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
        assert len(store.rows("SELECT * FROM operation_resolutions")) == 1
        assert len(api.calls) == 2  # preview and fresh read before local decision
        assert not api.account.writes_enabled
        assert store.claim() is None
        count = len(api.calls)
        await ui.click("ops:show:" + ident, owner=8)
        assert len(api.calls) == count and session.calls[-1].text == "Доступ закрыт"
    finally:
        await bot.session.close()
        store.close()


async def test_order_notes_search_stock_and_guarded_delivery_ui(tmp_path):
    from copy import deepcopy

    from app.database.store import now

    order = {
        "id": "order",
        "status": "PAID",
        "direction": "OUT",
        "has_problem": False,
        "is_automated": False,
        "created_at": now(),
        "item": {"id": "item", "name": "Product", "price": 100, "user": {"id": "seller"}},
        "user": {"username": "buyer_%"},
        "chat": {"id": "chat"},
        "logs": [],
    }

    class OrderAPI(CatalogAPI):
        account = SimpleNamespace(id="seller", writes_enabled=False)

        async def call(self, method, **kwargs):
            self.calls.append((method, kwargs))
            if method == "get_deal":
                return deepcopy(order)
            assert method == "get_chat"
            return {"id": "chat", "is_texting_allowed": True}

    store = Store(tmp_path / "orders-ui.sqlite")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    api = OrderAPI()
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.message("/start")
        await ui.click("order:order")
        await ui.click("od:problem:order")
        await ui.message("Local <note> for owner")
        token = (await state.get_data())["order_token"]
        ui.dp = dispatcher(store, api)
        await ui.click("od:apply:" + token)
        await ui.click("od:apply:" + token)
        assert store.rows("SELECT revision FROM order_annotations") == [{"revision": 1}]
        await ui.click("orderpage:0:PROBLEM")
        count = len(api.calls)
        await ui.click("od:search")
        await ui.message("buyer_%")
        assert "Всего 1" in session.calls[-1].text
        assert len(api.calls) == count
        await ui.click("od:stock:order")
        await ui.message("private code")
        token = (await state.get_data())["order_token"]
        assert "private code" not in session.calls[-1].text
        assert not store.rows("SELECT * FROM inventory")
        await ui.click("od:apply:" + token)
        assert len(store.rows("SELECT * FROM inventory")) == 1
        assert not store.rows("SELECT * FROM outbox")
        await ui.click("od:prepare:order")
        token = (await state.get_data())["order_token"]
        await ui.click("od:apply:" + token)
        assert "отключены" in session.last_panel.text and not store.rows("SELECT * FROM outbox")
        api.account.writes_enabled = True  # mock account only, never the real configuration
        await ui.click("od:apply:" + token)
        assert not store.rows("SELECT * FROM outbox")  # Local problem still blocks delivery.
        await ui.click("od:clear:order")
        token = (await state.get_data())["order_token"]
        await ui.click("od:apply:" + token)
        await ui.click("od:prepare:order")
        token = (await state.get_data())["order_token"]
        await ui.click("od:apply:" + token)
        await ui.click("od:apply:" + token)
        assert len(store.rows("SELECT * FROM outbox")) == 1
        assert store.rows("SELECT status FROM inventory") == [{"status": "RESERVED"}]
        assert all(method.startswith("get_") for method, _ in api.calls)
        assert "private code" not in str(store.rows("SELECT * FROM order_history"))
    finally:
        await bot.session.close()
        store.close()


async def test_fulfillment_checklist_router_requires_attestation_and_restores_confirmation(tmp_path):
    from copy import deepcopy

    from app.database.store import now

    class FulfillmentAPI(CatalogAPI):
        account = SimpleNamespace(id="seller", writes_enabled=False)

        async def call(self, method, **kwargs):
            self.calls.append((method, kwargs))
            assert method == "get_deal" and kwargs == {"deal_id": "deal"}
            return deepcopy(
                {
                    "id": "deal",
                    "status": "PAID",
                    "direction": "OUT",
                    "has_problem": False,
                    "is_automated": False,
                    "created_at": now(),
                    "item": {"id": "item", "seller_type": "USER", "user": {"id": "seller", "role": "USER"}},
                    "chat": {"id": "chat"},
                }
            )

    store = Store(tmp_path / "fulfillment-ui.sqlite")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    api = FulfillmentAPI()
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.message("/start")
        await ui.click("ff:start:deal")
        nonce = (await state.get_data())["fulfillment"]["nonce"]
        await ui.click("ff:" + nonce + ":continue")
        assert "Нужно лично" in session.calls[-1].text
        await ui.click("ff:" + nonce + ":check")
        nonce = (await state.get_data())["fulfillment"]["nonce"]
        await ui.click("ff:" + nonce + ":continue")
        nonce = (await state.get_data())["fulfillment"]["nonce"]
        ui.dp = dispatcher(store, api)
        await ui.click("ff:" + nonce + ":submit")
        assert "отключены" in session.last_panel.text and not store.rows("SELECT * FROM outbox")
        api.account.writes_enabled = True
        await ui.click("ff:" + nonce + ":submit")
        await ui.click("ff:" + nonce + ":submit")
        assert len(store.rows("SELECT * FROM outbox")) == 1
        assert store.rows("SELECT kind FROM outbox")[0]["kind"] == "fulfill"
        count = len(api.calls)
        await ui.click("ff:start:deal", owner=8)
        assert len(api.calls) == count
    finally:
        await bot.session.close()
        store.close()


async def test_automation_router_edits_local_policy_without_enabling_runtime(tmp_path):
    class PolicyAPI(CatalogAPI):
        async def call(self, method, **kwargs):
            self.calls.append((method, kwargs))
            assert method == "get_item"
            return {"id": kwargs["id"], "user": {"id": "seller"}}

    store = Store(tmp_path / "policy-ui.sqlite")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    api = PolicyAPI()
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.message("/start")
        await ui.click("automation")
        await ui.click("auto:item:item")
        nonce = (await state.get_data())["automation"]["nonce"]
        await ui.click(f"ap:{nonce}:mode:stock")
        nonce = (await state.get_data())["automation"]["nonce"]
        await ui.click(f"ap:{nonce}:toggle:enabled")
        nonce = (await state.get_data())["automation"]["nonce"]
        ui.dp = dispatcher(store, api)
        await ui.click(f"ap:{nonce}:save")
        assert "Изменения применяются после сохранения" in session.calls[-1].text
        assert store.rows("SELECT revision FROM delivery_policies") == [{"revision": 1}]
        assert not store.rows("SELECT * FROM outbox")
        await ui.click("auto:stop")
        assert store.rows("SELECT revision FROM delivery_policies") == [{"revision": 2}]
        count = len(api.calls)
        await ui.click("auto:item:item", owner=8)
        assert len(api.calls) == count
    finally:
        await bot.session.close()
        store.close()


async def test_promotions_settings_router_has_no_paid_execution(tmp_path):
    store = Store(tmp_path / "promotion-ui.sqlite")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    api = CatalogAPI()
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.message("/start")
        await ui.click("automation")
        await ui.click("promotions")
        nonce = (await state.get_data())["promotion"]["nonce"]
        await ui.click(f"pp:{nonce}:edit:interval_seconds")
        await ui.message("7200")
        nonce = (await state.get_data())["promotion"]["nonce"]
        ui.dp = dispatcher(store, api)
        await ui.click(f"pp:{nonce}:save")
        assert store.rows("SELECT revision FROM promotion_policy") == [{"revision": 1}]
        assert "Остановлено" in session.calls[-1].text
        await ui.click("promo:stop")
        assert store.rows("SELECT revision FROM promotion_policy") == [{"revision": 2}]
        assert not store.rows("SELECT * FROM promotion_expenses")
        assert not store.rows("SELECT * FROM outbox")
        await ui.click("promotions", owner=8)
        assert not api.calls
    finally:
        await bot.session.close()
        store.close()


async def test_queue_router_preserves_confirmation_and_blocks_foreign_access(tmp_path):
    from app.database.store import now
    from app.services.orders import Orders

    store = Store(tmp_path / "queue-ui.sqlite")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    api = CatalogAPI()
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.message("/start")
        Orders(store).ingest(
            {
                "has_problem": False,
                "is_automated": False,
                "id": "order",
                "status": "PAID",
                "direction": "OUT",
                "item": {"id": "item"},
            }
        )
        store.execute("INSERT INTO delivery_chains VALUES('order','item',7,1,'{}','ACTIVE',?)", (now(),))
        store.execute(
            "INSERT INTO delivery_chain_steps(order_id,position,kind,text) VALUES('order',0,'delivery_text','private code')"
        )
        await ui.click("aq:list:chains:0")
        await ui.click("aq:chain:order")
        assert "private code" not in session.calls[-1].text
        await ui.click("aq:stop:order")
        nonce = (await state.get_data())["chain_stop"]["nonce"]
        ui.dp = dispatcher(store, api)
        await ui.click("aq:apply:" + nonce)
        await ui.click("aq:apply:" + nonce)
        assert store.rows("SELECT status FROM delivery_chains") == [{"status": "CANCELLED"}]
        assert len(store.rows("SELECT * FROM order_history WHERE source='CHAIN_STOPPED'")) == 1
        await ui.click("aq:chain:order", owner=8)
        assert not api.calls
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()
        store.close()


async def test_future_order_policy_requires_separate_persisted_confirmation(tmp_path):
    class PolicyAPI(CatalogAPI):
        async def call(self, method, **kwargs):
            assert method == "get_item"
            return {"id": "item", "user": {"id": "seller"}}

    store = Store(tmp_path / "future-policy-ui.sqlite")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    api = PolicyAPI()
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))

    async def press(command):
        nonce = (await state.get_data())["automation"]["nonce"]
        await ui.click("ap:" + nonce + ":" + command)

    try:
        await ui.message("/start")
        await ui.click("auto:item:item")
        await press("mode:stock")
        await press("toggle:enabled")
        await press("toggle:auto_enroll")
        await press("save")
        assert "будущих продаж" in session.calls[-1].text
        assert not store.rows("SELECT * FROM delivery_policies")
        nonce = (await state.get_data())["automation"]["nonce"]
        ui.dp = dispatcher(store, api)
        await ui.click("ap:" + nonce + ":arm")
        await ui.click("ap:" + nonce + ":arm")
        assert store.rows("SELECT revision FROM delivery_policies") == [{"revision": 1}]
        assert not store.rows("SELECT * FROM delivery_chains")
        assert not store.rows("SELECT * FROM outbox")
        assert not api.account.writes_enabled
    finally:
        await bot.session.close()
        store.close()
