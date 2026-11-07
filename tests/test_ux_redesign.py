"""User-facing transitions through the production Dispatcher, with no live API writes."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store
from app.telegram.callback_ack import navigation
from app.telegram.home_panel import HomePanel
from app.telegram.keyboards import main_menu
from app.telegram.presentation import status
from app.telegram.storage import SQLiteStorage
from tests.test_item_panel import item
from tests.test_telegram_integration import (
    CatalogAPI,
    Interaction,
    TelegramSession,
    dispatcher,
    existing_legacy_delivery_draft,
)


class ShopAPI(CatalogAPI):
    def __init__(self):
        super().__init__()
        self.account = SimpleNamespace(
            id="seller", writes_enabled=False, automation_enabled=False, connection_verified=False
        )
        self.value = item()

    async def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == "get_item":
            return deepcopy(self.value)
        if method == "get_my_items":
            return {"items": [deepcopy(self.value)], "page_info": {"has_next_page": False}}
        if method == "get_chats":
            return {
                "chats": [{"id": "chat", "user": {"username": "Buyer"}}],
                "page_info": {"has_next_page": False},
            }
        if method == "get_deal":
            return {
                "id": kwargs["deal_id"],
                "direction": "OUT",
                "status": "PAID",
                "has_problem": False,
                "is_automated": False,
                "item": {"id": "lot", "name": "<Paid product>", "price": 100, "user": {"id": "seller"}},
                "user": {"username": "Buyer"},
                "chat": {"id": "chat"},
            }
        if method == "get_chat":
            return {"id": "chat", "is_texting_allowed": True}
        if method == "get_chat_messages":
            return {
                "messages": [
                    {
                        "id": "secret-message-id",
                        "text": "<hello>",
                        "user": {"username": "Buyer"},
                        "created_at": "2026-10-31T00:00:00Z",
                    }
                ],
                "page_info": {"has_next_page": False},
            }
        raise AssertionError(method)


@pytest.fixture
async def ux(tmp_path):
    store, session, api = Store(tmp_path / "ux.sqlite"), TelegramSession(), ShopAPI()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(store, api))
    await ui.message("/start")
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    yield SimpleNamespace(store=store, api=api, session=session, bot=bot, ui=ui, state=state)
    await bot.session.close()
    store.close()


def buttons(method):
    return {
        b.callback_data: b.text for row in method.reply_markup.inline_keyboard for b in row if b.callback_data
    }


async def test_gift_picker_server_drafts_keep_selection_context_after_restart(ux):
    r = ux
    await r.ui.click("gft:bindings")
    choices = buttons(r.session.calls[-1])
    route = next(k for k, v in choices.items() if "Выбрать товар" in v)
    await r.ui.click(route)
    assert "itemfilter:DRAFT" in buttons(r.session.calls[-1])
    assert "gft:item:lot" in buttons(r.session.calls[-1])
    assert "drafts" not in buttons(r.session.calls[-1])
    r.ui.dp = dispatcher(r.store, r.api)
    r.api.value["status"] = "DRAFT"
    await r.ui.click("itemfilter:DRAFT")
    assert (await r.state.get_data())["gift_picker"] is True
    assert "gft:item:lot" in buttons(r.session.calls[-1])
    assert r.api.calls[-1][1]["statuses"][0].name == "DRAFT"
    await r.ui.click("il:search")
    await r.ui.message("Item")
    assert "gft:item:lot" in buttons(r.session.calls[-1])
    await r.ui.click("il:reset")
    assert "gft:item:lot" in buttons(r.session.calls[-1])
    await r.ui.click("home")
    r.api.value["status"] = "APPROVED"
    await r.ui.click("items")
    assert "item:lot" in buttons(r.session.calls[-1])
    assert "itemfilter:DRAFT" in buttons(r.session.calls[-1])
    assert not r.store.rows("SELECT * FROM outbox")


async def test_gift_picker_opens_server_draft_binding_without_enabling_delivery(ux):
    from app.gifts.mtproto import PersonalGiftSender
    from app.gifts.settings import GiftSettings
    from app.services.account_binding import bind_account
    from tests.test_gifts import MTProtoTransport

    r = ux
    r.api.gift_settings = GiftSettings(
        1, "test-only", "data/test", "stars", "gift", "username", False, 100, 300
    )
    r.api.gift_sender = PersonalGiftSender(r.api.gift_settings, client=MTProtoTransport())
    bind_account(r.store, r.api.account.id)
    r.api.value.update(
        status="DRAFT",
        description="Gift\ngift: zombie_bear, rose",
        category={"id": "stars"},
        obtaining_type={"id": "gift"},
    )

    async def schema(*args, **kwargs):
        yield [{"id": "username", "type": "OBTAINING_DATA", "required": True}]

    r.api.pages = schema
    r.ui.dp = dispatcher(r.store, r.api)
    await r.ui.click("gft:bindings")
    await r.ui.click("gft:choose")
    await r.ui.click("itemfilter:DRAFT")
    await r.ui.click("gft:item:lot")
    assert any(":giftpick:heart" in key for key in buttons(r.session.calls[-1]))
    assert (await r.state.get_data())["logical_setup"]["handler"] == "telegram_gifts"
    assert "gift: zombie_bear, rose" in r.api.value["description"]  # Selection never edits Playerok.
    assert not r.store.rows("SELECT * FROM gift_listings")
    assert not r.store.rows("SELECT * FROM outbox")


async def test_home_exactly_six_no_fake_connection_and_orders_attention(ux):
    r = ux
    assert set(buttons(r.session.calls[-1])) == {
        "items",
        "orders",
        "chats",
        "promotions",
        "stats:7",
        "settings",
    }
    assert "🟢" not in r.session.calls[-1].text
    assert not r.api.calls
    r.store.enqueue("uncertain", "reply", "chat", {}, actor=7)
    r.store.execute("UPDATE outbox SET status='UNCERTAIN'")
    await r.ui.click("home")
    assert "orders" in buttons(r.session.calls[-1])
    assert "требующ" in r.session.calls[-1].text
    await r.ui.click("attention")
    assert "ops:list:UNCERTAIN:0" in buttons(r.session.calls[-1])
    assert not r.api.calls


async def test_home_expired_success_is_not_fresh(ux):
    r = ux
    r.api.account.connection_verified = True
    r.api.connection = SimpleNamespace(
        state={"status": "CONNECTED", "last_success": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}
    )
    assert "🟢" not in HomePanel(r.store, r.api).home()[0]
    await r.ui.click("more")
    await r.ui.click("home")
    assert "аккаунт подтверждён" in r.session.calls[-1].text
    assert "нужно проверить" not in r.session.calls[-1].text
    assert r.api.account.connection_verified and not r.api.calls
    r.api.connection.state["last_success"] = datetime.now(UTC).isoformat()
    assert "🟢" in HomePanel(r.store, r.api).home()[0]


async def test_more_and_all_tools_have_real_routes_without_external_calls(ux):
    r = ux
    await r.ui.click("more")
    assert "tools" in buttons(r.session.calls[-1])  # old More redirects to Settings
    assert "gft:home" not in buttons(r.session.calls[-1])
    for route in (
        "tools",
        "tools:operations",
        "tools:delivery",
        "tools:data",
        "tools:promotion",
        "settings",
        "gft:home",
        "gft:history",
        "gft:settings",
        "automation",
        "promotions",
        "stats:7",
        "sr:7",
    ):
        await r.ui.click(route)
        assert "❗" not in r.session.calls[-1].text, route
        assert any(
            k in {"home", "more", "tools", "gft:home", "stats:7"} or k.startswith("nav:back:")
            for k in buttons(r.session.calls[-1])
        ), route
    assert not r.api.calls
    assert not r.store.rows("SELECT * FROM outbox")


async def test_item_compact_sections_all_old_actions_and_safe_delete(ux):
    r = ux
    await r.ui.click("items")
    assert {"create", "itemfilter:APPROVED", "itemfilter:ARCHIVE", "itemfilter:DRAFT"} <= buttons(
        r.session.calls[-1]
    ).keys()
    await r.ui.click("item:lot")
    card = r.session.calls[-1]
    assert "&lt;Item&gt;" in card.text and "Опубликован" in card.text
    assert "<Description>" not in card.text and "Позиция" not in card.text
    assert {"finance:lot", "payp:item:lot", "iv:edit:lot", "iv:delivery:lot"} <= buttons(card).keys()
    await r.ui.click("iv:edit:lot")
    assert {"iv:other:lot", "iv:history:lot"} <= buttons(r.session.calls[-1]).keys()
    assert {"discount:lot", "editdesc:lot", "editname:lot", "editphoto:lot", "editfields:lot"} <= buttons(
        r.session.calls[-1]
    ).keys()
    r.store.set_setting("playerok:account_id", r.api.account.id)
    await r.ui.click("iv:delivery:lot")
    assert "lp:method:lot" in buttons(r.session.calls[-1])
    assert "stock:view:lot" not in buttons(r.session.calls[-1])  # No unrelated stock before selecting it.
    assert "gft:item:lot" not in buttons(r.session.calls[-1])
    await r.ui.click("iv:other:lot")
    assert {"delete:lot", "iv:details:lot"} <= buttons(r.session.calls[-1]).keys()
    await r.ui.click("delete:lot")
    pending = await r.state.get_data()
    token = pending["confirm_token"]
    assert not r.store.rows("SELECT * FROM outbox")
    await r.ui.click("home")
    await r.ui.click("commit:" + token)
    assert "устарело" in r.session.calls[-1].text
    assert not r.store.rows("SELECT * FROM outbox")


async def test_filters_survive_card_home_and_dispatcher_restart(ux):
    r = ux
    await r.ui.click("itemfilter:APPROVED")
    await r.ui.click("item:lot")
    await r.ui.click("home")
    r.ui.dp = dispatcher(r.store, r.api)
    await r.ui.click("items")
    assert (await r.state.get_data())["item_filters"]["status"] == "APPROVED"
    assert r.api.calls[-1][0] == "get_my_items"
    assert r.api.calls[-1][1]["statuses"][0].name == "APPROVED"


async def test_chat_chronology_without_ids_and_reply_confirmation(ux):
    r = ux
    r.store.set_setting("playerok:account_id", r.api.account.id)
    await r.ui.click("chat:chat")
    shown = r.session.calls[-1]
    assert "secret-message-id" not in shown.text
    assert "&lt;hello&gt;" in shown.text
    labels = buttons(shown)
    assert set(labels.values()) == {"✉️ Ответить", "✖️ Закрыть"}
    reply = next(key for key in labels if key.startswith("compose:"))
    source = r.store.setting("navigation:123:7")["id"]
    await r.ui.click(reply, message_id=source)
    await r.ui.message("<my reply>")
    # Read-only installation never enqueues or pretends the reply succeeded.
    assert not r.store.rows("SELECT * FROM outbox")
    assert "не разрешает" in r.session.last_panel.text


async def test_gifts_stop_visible_and_no_implicit_enable(ux):
    r = ux
    r.store.set_setting("gifts:stopped", False)
    await r.ui.click("gft:home")
    assert "gft:stop" in buttons(r.session.calls[-1])
    assert "gft:history" in buttons(r.session.calls[-1])
    await r.ui.stop("gft:stop")
    assert r.store.setting("gifts:stopped") is True
    assert not r.api.calls
    assert not r.store.rows("SELECT * FROM outbox")


async def test_nested_local_settings_survive_restart_and_rotate_nonce(ux):
    r = ux
    await existing_legacy_delivery_draft(r.state, r.api.account.id, "lot")
    await r.ui.click("auto:item:lot")
    first = (await r.state.get_data())["automation"]["nonce"]
    await r.ui.click(f"ap:{first}:advanced")
    assert "Будущие заказы автоматически" in buttons(r.session.calls[-1]).values()
    r.ui.dp = dispatcher(r.store, r.api)
    data = (await r.state.get_data())["automation"]
    await r.ui.click(f"ap:{data['nonce']}:basic")
    assert "Дополнительные параметры" in buttons(r.session.calls[-1]).values()
    await r.ui.click(f"ap:{first}:save")
    assert "устарели" in r.session.calls[-1].text
    assert not r.store.rows("SELECT * FROM delivery_policies")
    assert not r.store.rows("SELECT * FROM outbox")


async def test_new_navigation_denies_foreign_owner_and_group(ux):
    r = ux
    before = len(r.api.calls)
    for action in ("more", "tools:data", "iv:delivery:lot", "compose:chat"):
        await r.ui.click(action, owner=8)
    await r.ui.message("/start", owner=8, group=True)
    assert len(r.api.calls) == before
    assert not r.store.rows("SELECT * FROM outbox")


def test_unknown_states_never_become_success_or_pending():
    assert "не распознан" in status("FUTURE_UNKNOWN_STATUS")
    assert "неизвестен" in status("UNCERTAIN")
    assert "Продавец" in status("SENT", "order")
    assert len([b for row in main_menu().inline_keyboard for b in row]) == 6
    assert not navigation("gft:confirmretry:old")
    assert not navigation("commit:old")


async def test_order_compact_card_details_retain_all_actions(ux):
    from app.services.orders import Orders

    r = ux
    await r.ui.click("order:paid")
    assert "od:prepare:paid" not in buttons(r.session.calls[-1])
    Orders(r.store).add_stock("lot", ["not-shown-private-unit"])
    await r.ui.click("order:paid")
    shown = r.session.calls[-1]
    assert "&lt;Paid product&gt;" in shown.text and "Оплачен" in shown.text
    assert {"compose:chat", "ff:start:paid", "od:prepare:paid", "od:details:paid", "orders"} <= buttons(
        shown
    ).keys()
    assert "Версия" not in shown.text and "ID:" not in shown.text
    await r.ui.click("od:details:paid")
    assert {
        "od:history:paid:0",
        "od:note:paid",
        "od:problem:paid",
        "pf:card:paid",
        "od:stock:paid",
    } <= buttons(r.session.calls[-1]).keys()
    assert any(
        b.url == "https://playerok.com/deal/paid"
        for row in r.session.calls[-1].reply_markup.inline_keyboard
        for b in row
    )
    assert not r.store.rows("SELECT * FROM outbox")


async def test_navigation_from_notification_edits_its_source_without_deleting_evidence(ux):
    r = ux
    from app.services.orders import Orders

    Orders(r.store).ingest_message("chat", {"id": "incoming", "text": "Original proof"})
    r.store.execute(
        "INSERT INTO notifications(event_key,owner_id,telegram_message_id,status) VALUES('message:incoming',7,1,'SUCCEEDED')"
    )
    evidence = r.store.rows("SELECT * FROM chat_messages")
    for action in ("home", "more", "tools", "chat:chat", "iv:other:lot"):
        await r.ui.click(action)
        assert not any(
            m.__api_method__ == "deleteMessage" and getattr(m, "message_id", None) == 1
            for m in r.session.calls
        )
    assert any(m.__api_method__ == "editMessageText" and m.message_id == 1 for m in r.session.calls)
    assert all(m.message_id == 1 for m in r.session.calls if m.__api_method__ == "editMessageText")
    assert all(row in r.store.rows("SELECT * FROM chat_messages") for row in evidence)
    assert r.store.rows("SELECT telegram_message_id FROM notifications")[0]["telegram_message_id"] == 1


async def test_navigation_after_failed_ack_recovers_without_writes(tmp_path):
    from aiogram.exceptions import TelegramNetworkError

    from tests.test_telegram_transport import FailingAckSession

    store, api = Store(tmp_path / "ack.sqlite"), ShopAPI()
    store.claim_first_owner(7)
    session = FailingAckSession(TelegramNetworkError)
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(store, api))
    try:
        for action in ("more", "tools:data", "iv:edit:lot", "gft:settings"):
            await ui.click(action)
            assert r"Нажатие не подтверждено" not in session.calls[-1].text
            assert session.calls[-1].__api_method__ in {"sendMessage", "editMessageText"}
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await bot.session.close()
        store.close()


async def test_chat_list_uses_real_cached_snippet_and_time(ux):
    r = ux
    await r.ui.click("chat:chat")
    await r.ui.click("chats")
    shown = r.session.calls[-1]
    assert "03:00" in buttons(shown)["chat:chat"]  # Moscow presentation
    assert "<hello>" in buttons(shown)["chat:chat"]
    assert "сохранённой истории" in shown.text
    assert not r.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("changed", [{"has_problem": True}, {"is_automated": True}, {"status": "SENT"}])
async def test_order_does_not_offer_ineligible_stock_delivery(ux, changed):
    from app.services.orders import Orders

    r = ux
    Orders(r.store).add_stock("lot", ["private-unit"])
    original = r.api.call

    async def call(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_deal":
            result.update(changed)
        return result

    r.api.call = call
    await r.ui.click("order:paid")
    assert "od:prepare:paid" not in buttons(r.session.calls[-1])
    assert "private-unit" not in r.session.calls[-1].text
    assert not r.store.rows("SELECT * FROM outbox")
