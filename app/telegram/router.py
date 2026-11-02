import asyncio
import json
import secrets
import sqlite3
from datetime import UTC
from html import escape
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile

from app.database.store import Conflict
from app.localvpn import active_proxy
from app.playerok.adapter import APIError
from app.services.discounts import discount_plan
from app.services.products import ValidationError, listing_price, listing_text, priority_price_basis
from app.services.replies import Replies
from app.telegram.agreements_panel import AgreementsPanel
from app.telegram.auto_replies_panel import AutoRepliesPanel
from app.telegram.automation_panel import AutomationPanel
from app.telegram.automation_queue import AutomationQueue
from app.telegram.callback_ack import ACTION_MAX_WAIT, acknowledge, navigation
from app.telegram.chat_cards import ChatCards
from app.telegram.chat_panel import ChatPanel
from app.telegram.code_migration_panel import CodeMigrationPanel
from app.telegram.drafts_panel import DraftsPanel
from app.telegram.finance_panel import FinancePanel
from app.telegram.formatting import escaped
from app.telegram.fulfillment_panel import FulfillmentPanel
from app.telegram.gifts_panel import GiftsPanel
from app.telegram.home_panel import HomePanel
from app.telegram.input_cleanup import InputCleanup
from app.telegram.interface_cleanup_panel import InterfaceCleanupPanel
from app.telegram.item_panel import ItemPanel
from app.telegram.keyboards import keyboard, main_menu
from app.telegram.launch_panel import LaunchPanel
from app.telegram.listings_panel import ListingsPanel
from app.telegram.logical_products_panel import LogicalProductsPanel
from app.telegram.navigation import Navigation
from app.telegram.operation_results import OperationResults
from app.telegram.operations_panel import OperationsPanel
from app.telegram.order_finance_panel import OrderFinancePanel
from app.telegram.orders_panel import OrdersPanel
from app.telegram.paid_promotions_panel import PaidPromotionsPanel
from app.telegram.platform_panel import PlatformPanel
from app.telegram.presentation import error_text, local_time
from app.telegram.profile_panel import ProfilePanel
from app.telegram.promotions_panel import PromotionsPanel
from app.telegram.quick_shop_panel import QuickShopPanel
from app.telegram.settings_panel import SettingsPanel
from app.telegram.statistics_panel import StatisticsPanel
from app.telegram.stock_panel import StockPanel
from app.telegram.storage import replace_context
from app.telegram.wizard import Wizard
from playerokapi.enums import ItemStatuses


async def display(message, text, markup, edit=False):
    if edit and message.text:
        try:
            return await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as error:
            if "message is not modified" in error.message.lower():
                return
            if not any(
                fragment in error.message.lower()
                for fragment in ("message to edit not found", "message can't be edited")
            ):
                raise
    return await message.answer(text, reply_markup=markup)


def create_router(store, api, products, synchronizer):
    router = Router()
    router.message.middleware(InputCleanup(store))
    navigator = Navigation(store)
    wizard = Wizard(store, api, products)
    finance = FinancePanel(store, api, products)
    chats = ChatPanel(store, api)
    operations = OperationsPanel(store, api)
    orders_panel = OrdersPanel(store, api)
    fulfillment_panel = FulfillmentPanel(store, api)
    automation_panel = AutomationPanel(store, api)
    platform_panel = PlatformPanel(store, api)
    auto_replies_panel = AutoRepliesPanel(store, api)
    promotions_panel = PromotionsPanel(store, api)
    paid_promotions = PaidPromotionsPanel(store, api)
    automation_queue = AutomationQueue(store, api)
    statistics_panel = StatisticsPanel(store)
    settings_panel = SettingsPanel(store, api)
    drafts_panel = DraftsPanel(store)
    interface_cleanup = InterfaceCleanupPanel(store)
    order_finance_panel = OrderFinancePanel(store, api)
    profile_panel = ProfilePanel(api, store)
    home_panel = HomePanel(store, api)
    item_panel = ItemPanel(api, store)
    listings_panel = ListingsPanel(api, store)
    logical_panel = LogicalProductsPanel(store, api)
    code_migration_panel = CodeMigrationPanel(store, api)
    stock_panel = StockPanel(store, api)
    gifts_panel = GiftsPanel(store, api)
    launch_panel = LaunchPanel(store, api, gifts_panel.service)
    operation_results = OperationResults(store)
    quick_shop = QuickShopPanel(store, api, wizard, gifts_panel.service)
    agreements_panel = AgreementsPanel(store, api, products)

    @router.message(CommandStart())
    async def start(message, state):
        data = await state.get_data()
        for key in ("order_search", "order_filter"):
            if key in data:
                store.set_setting(f"ui:{message.from_user.id}:{key}", data[key])
        await state.clear()
        panel = store.setting(navigator.key(message.bot, message.from_user.id), {})
        if panel.get("sending") or panel.get("editing"):
            return await navigator.recovery_prompt(message, message.from_user.id)
        message, _ = await navigator.enter(message, message.from_user.id, state, "home")
        text, markup = home_panel.home()
        await message.answer(text, reply_markup=markup)

    @router.message(Command("status"))
    async def status_command(message, state):
        automation_panel.service.orders.owner(message.from_user.id)
        await state.clear()
        message, _ = await navigator.enter(message, message.from_user.id, state, "status:view:0")
        try:
            text, markup = await quick_shop.status(message.from_user.id, state)
        except (APIError, ValidationError, Conflict):
            text, markup = (
                "⚪ Не удалось подтвердить активные товары. Проверьте подключение и обновите статус.",
                keyboard([[("🔄 Проверить снова", "status:view:0")]]),
            )
        await message.answer(text, reply_markup=markup)

    @router.message(Command("cancel"))
    async def cancel_chat_reply(message, state):
        data = await state.get_data()
        if await state.get_state() == "chatcard:reply":
            store.execute(
                "UPDATE chat_cards SET hint='' WHERE token=? AND owner_id=?",
                (data.get("chat_card"), message.from_user.id),
            )
            await state.clear()
            await ChatCards(store, message.bot).flush()
            return
        await state.clear()

    @router.message(Command("panel_reset"))
    async def reset_navigation(message, state):
        key = navigator.key(message.bot, message.from_user.id)
        panel = store.setting(key, {})
        await state.clear()  # Never restore a confirmation from the abandoned UI.
        if panel.get("sending") or panel.get("editing"):
            return await navigator.recovery_prompt(message, message.from_user.id)
        if panel.get("id"):
            store.set_setting(key, {"route": None, "history": [], "abandoned_id": panel["id"]})
        message, _ = await navigator.enter(message, message.from_user.id, state, "home")
        text, markup = home_panel.home()
        await message.answer(text, reply_markup=markup)

    @router.callback_query()
    async def callback(query, state, callback_ack=None):
        import time

        ack = callback_ack if callback_ack is not None else await acknowledge(query)
        action = query.data or ""
        owner = query.from_user.id
        message = query.message
        if message is None or not hasattr(message, "answer"):
            return
        if action.startswith(("cc:", "chat:", "compose:", "reply:", "readchat:", "chatmore:", "chatclose:")):
            cards = ChatCards(store, message.bot)
            token = action.split(":")[-1]
            try:
                if action.startswith("cc:"):
                    if ack["status"] != "ok":
                        return
                    if action.startswith("cc:close:"):
                        await cards.close(token, owner, message.message_id)
                        if (await state.get_data()).get("chat_card") == token:
                            await state.clear()
                    elif action.startswith("cc:reply:"):
                        await cards.compose(token, owner, message.message_id, state, api)
                    return
                ident = action.split(":", 1)[1]
                if action.startswith("chatclose:"):
                    rows = store.rows(
                        "SELECT * FROM chat_cards WHERE chat_id=? AND owner_id=? AND account_id=?",
                        (ident, owner, cards.account),
                    )
                    if rows and rows[0]["telegram_message_id"] == message.message_id:
                        await cards.close(rows[0]["token"], owner, message.message_id)
                    return
                previous = (await state.get_data()).get("chat_card")
                await state.clear()
                if previous:
                    store.execute(
                        "UPDATE chat_cards SET hint='' WHERE token=? AND owner_id=?", (previous, owner)
                    )
                await chats.show("chat:" + ident, state)  # API history only; never mark read.
                cards.touch(owner, ident)
                await cards.flush()
                if action.startswith(("compose:", "reply:")) and ack["status"] == "ok":
                    row = store.rows(
                        "SELECT * FROM chat_cards WHERE owner_id=? AND account_id=? AND chat_id=?",
                        (owner, cards.account, ident),
                    )[0]
                    if row["state"] == "OPEN":
                        await cards.compose(row["token"], owner, row["telegram_message_id"], state, api)
            except (ValueError, APIError) as error:
                if not action.startswith("cc:") and store.rows(
                    "SELECT 1 FROM chat_messages WHERE chat_id=? LIMIT 1", (token,)
                ):
                    cards.touch(owner, token)
                    store.execute(
                        "UPDATE chat_cards SET hint=? WHERE chat_id=? AND owner_id=? AND account_id=?",
                        (
                            "Историю Playerok обновить не удалось. Показаны сохранённые сообщения.",
                            token,
                            owner,
                            cards.account,
                        ),
                    )
                store.execute(
                    "UPDATE chat_cards SET hint=? WHERE token=? AND owner_id=?",
                    (error_text(error), token, owner),
                )
                await cards.flush()
            return
        if not navigator.allowed(message, owner, action):
            return await navigator.feedback(message, owner, "Экран закрыт или устарел. Откройте /start")
        if not navigation(action) and (
            ack["status"] != "ok"
            or time.monotonic() - ack.get("admitted_at", ack["checked_at"]) > ACTION_MAX_WAIT
        ):
            return await navigator.feedback(
                message,
                owner,
                "Нажатие не подтверждено. Действие не выполнено; текущий экран сохранён. Повторно откройте его после восстановления связи.",
            )
        preserve = bool(
            store.rows(
                "SELECT 1 FROM notifications WHERE owner_id=? AND telegram_message_id=? "
                "UNION ALL SELECT 1 FROM operation_receipts WHERE owner_id=? AND telegram_message_id=? "
                "UNION ALL SELECT 1 FROM chat_cards WHERE owner_id=? AND telegram_message_id=? LIMIT 1",
                (owner, message.message_id, owner, message.message_id, owner, message.message_id),
            )
        )
        try:
            if action.startswith("nav:recover:"):
                await navigator.recover(message, owner, state, action.split(":")[-1])
                action = "home"
            message, action = await navigator.enter(message, owner, state, action)
            if message is None:
                return
            # Persist only harmless list preferences, never confirmation tokens or pending actions.
            view_data = await state.get_data()
            for key in ("order_search", "order_filter"):
                if key in view_data:
                    store.set_setting(f"ui:{owner}:{key}", view_data[key])
            if (
                action
                in {
                    "profile",
                    "profile:check",
                    "profile:details",
                    "diagnostics",
                    "backup",
                }
                or action.startswith(
                    (
                        "item:",
                        "iv:",
                        "order:",
                        "chat:",
                        "result:",
                        "delivered:",
                        "stats:",
                        "sr:",
                        "st:",
                        "sd:",
                        "sp:",
                        "stock:list:",
                        "ops:show:",
                        "aq:list:",
                        "aq:chain:",
                        "aq:target:",
                        "aq:evaluation:",
                        "gft:item:",
                        "gft:job:",
                        "gft:details:",
                        "gft:list:",
                        "gft:catalog",
                        "gft:bindings",
                        "gft:reconcile:",
                        "gft:refundcheck:",
                    )
                )
            ) and not message.navigation_restoring:
                await state.clear()
            if action == "home":
                await state.clear()
                text, markup = home_panel.home()
                return await display(message, text, markup, not preserve)
            if action in {"more", "attention", "tools"} or action.startswith("tools:"):
                await state.clear()
                text, markup = home_panel.show(action, owner)
            elif action.startswith("codes:"):
                text, markup = await code_migration_panel.callback(action, owner, state)
            elif action == "create":
                text, markup = await wizard.start(owner, state)
            elif action.startswith(("rd:check:", "rd:balance:")):
                text, markup = await item_panel.diagnostic(action.split(":", 2)[2], owner)
            elif action.startswith("lp:"):
                text, markup = await logical_panel.callback(action, owner, state)
            elif action.startswith("agv:"):
                text, markup = await agreements_panel.callback(action, state)
            elif action.startswith("gft:"):
                text, markup = await gifts_panel.callback(action, owner, state)
            elif action.startswith(("launch:", "ln:")):
                text, markup = await launch_panel.callback(action, owner, state)
            elif finance.handles(action):
                text, markup = await finance.callback(action, owner, state)
            elif action.startswith("pf:"):
                text, markup = await order_finance_panel.callback(action, owner, state)
            elif action.startswith("stock:"):
                text, markup = await stock_panel.callback(action, owner, state)
            elif action.startswith("result:"):
                parts = action.split(":")
                text, markup = (
                    operation_results.listing(owner, int(parts[2]))
                    if parts[1] == "list"
                    else operation_results.show(owner, parts[2])
                )
            elif action.startswith("delivered:"):
                text, markup = orders_panel.delivered(owner, int(action.split(":")[1]))
            elif action.startswith("ic:"):
                text, markup = await interface_cleanup.callback(action, owner, state, message)
            elif action == "quickreplies" or action.startswith(("quick:", "replypreview:")):
                await state.clear()
                text, markup = (
                    "Шаблоны больше не используются. Откройте переписку и нажмите «Ответить».",
                    keyboard([[("💬 Сообщения", "chats")]]),
                )
            elif action.startswith("ops:"):
                text, markup = await operations.callback(action, owner, state)
            elif action.startswith("aq:"):
                text, markup = await automation_queue.callback(action, owner, state)
            elif action.startswith("ar:"):
                text, markup = await auto_replies_panel.callback(action, owner, state, message)
            elif action.startswith("payp:"):
                text, markup = await paid_promotions.callback(action, owner, state)
            elif action == "promo:catalog":
                if not message.navigation_restoring:
                    await state.clear()
                    await state.set_data({"promotion_picker": True})
                text, markup = await listings_panel.show("items", state, restore=message.navigation_restoring)
            elif action == "promotions" or action.startswith(("promo:", "pp:", "pi:")):
                text, markup = await promotions_panel.callback(action, owner, state)
            elif action.startswith("relist:"):
                text, markup = await quick_shop.relist(action, owner, state)
            elif action.startswith("status:view:"):
                text, markup = await quick_shop.status(owner, state, int(action.split(":")[-1]))
            elif action.startswith("platform:"):
                text, markup = await platform_panel.callback(action, owner, state)
            elif action == "automation" or action.startswith(("auto:", "ap:")):
                text, markup = await automation_panel.callback(action, owner, state)
            elif action.startswith("ff:"):
                text, markup = await fulfillment_panel.callback(action, owner, state)
            elif action.startswith("w:"):
                text, markup = await wizard.callback(owner, state, action, message=message)
            elif action == "cancel_wizard":
                data = await state.get_data()
                if data.get("draft_id"):
                    store.execute(
                        "UPDATE drafts SET state='CANCELLED' WHERE id=? AND owner_id=? AND state='DRAFT'",
                        (data["draft_id"], owner),
                    )
                await state.clear()
                text, markup = "Сценарий отменён. Запись оставлена в локальной истории.", main_menu()
            elif action == "drafts" or action.startswith("dl:"):
                text, markup = await drafts_panel.callback(action, owner, state)
            elif action.startswith("draft:"):
                ident = action.split(":", 1)[1]
                draft = store.get_draft(ident, owner)
                if draft["payload"].get("launch_target"):
                    if draft["state"] == "CANCELLED":
                        ident = await launch_panel.service.open(owner, draft["payload"]["launch_target"])
                    text, markup = await launch_panel.show(owner, state, ident)
                elif draft["state"] == "DRAFT":
                    await replace_context(state, "wizard", {"draft_id": ident, "revision": draft["revision"]})
                    text, markup = await wizard.screen(owner, state)
                else:
                    binding = store.rows("SELECT remote_id FROM bindings WHERE draft_id=?", (ident,))
                    if not binding and draft["payload"].get("edit_target"):
                        binding = [{"remote_id": draft["payload"]["edit_target"]}]
                    rows = [[("Открыть серверный лот", "item:" + binding[0]["remote_id"])]] if binding else []
                    text, markup = "Состояние: " + escape(draft["state"]), keyboard(rows)
            elif action in {"items", "chats"}:
                restoring = message.navigation_restoring
                if not restoring:
                    await state.clear()
                    filters = (
                        store.setting(f"ui:{owner}:item_filters", {})
                        if store.setting(f"ui:{owner}:item_filters_pinned", False)
                        else {"status": "APPROVED"}
                    )
                    if action == "items":
                        await state.update_data(item_filters=filters)
                text, markup = await listings_panel.show(action, state, restore=restoring)
            elif action == "itemfilters":
                await state.set_state(None)
                names = {
                    "APPROVED": "Активные",
                    "DRAFT": "Черновики",
                    "SOLD": "Проданные",
                    "EXPIRED": "Истёкшие",
                    "PENDING_APPROVAL": "На проверке",
                }
                rows = [
                    [(label, "itemfilter:" + value)]
                    for value, label in names.items()
                    if value in ItemStatuses.__members__
                ]
                rows.append([("Все статусы", "itemfilter:ALL")])
                rows.extend(
                    [
                        [("Название", "il:search"), ("Цена", "il:price"), ("Категория", "il:pickcategory")],
                        [("Категория · ввод ID / снять", "il:category")],
                        [("PREMIUM", "il:PREMIUM"), ("Обычные", "il:DEFAULT"), ("Любой приоритет", "il:ALL")],
                        [("С настроенной выдачей", "il:delivery"), ("С проблемами", "il:problems")],
                        [("Сбросить фильтры", "il:reset"), ("К списку", "il:back")],
                        [("Запомнить фильтр", "il:remember")],
                    ]
                )
                text, markup = (
                    "📦 Фильтры товаров\nПоиск по названию, коду, выдаче и проблемам проверяет доступный каталог. Незавершённая загрузка отмечается отдельно. Остальные фильтры уточняют список.",
                    keyboard(rows),
                )
            elif action.startswith("itemfilter:"):
                value = action.split(":", 1)[1]
                if value not in {"ALL", "ARCHIVE"} and value not in ItemStatuses.__members__:
                    raise ValidationError("Неизвестный статус товара")
                filters = (await state.get_data()).get("item_filters", {})
                filters["status"] = None if value in {"ALL", "ARCHIVE"} else value
                filters["archive"] = value == "ARCHIVE"
                await state.set_data(
                    {
                        "item_filters": filters,
                        **{
                            key: True
                            for key in ("gift_picker", "promotion_picker")
                            if (await state.get_data()).get(key)
                        },
                    }
                )
                await state.set_state(None)
                text, markup = await listings_panel.show("items", state)
            elif action == "il:back":
                await state.set_state(None)
                text, markup = await listings_panel.show("items", state)
            elif action.startswith("il:"):
                text, markup = await listings_panel.filter_action(action, state)
            elif action.startswith("icp:"):
                selected = await listings_panel.category_picker.action(action, state)
                text, markup = selected if selected is not None else await listings_panel.show("items", state)
            elif action.startswith("listnext:"):
                parts = action.split(":")
                if len(parts) != 3:
                    raise ValidationError("Список устарел. Откройте его заново")
                text, markup = await listings_panel.show(parts[1], state, nonce=parts[2])
            elif action.startswith(("item:", "iv:")):
                if action.startswith("item:"):
                    ident, section = action.split(":", 1)[1], "card"
                else:
                    _, section, ident = action.split(":", 2)
                text, markup = await item_panel.screen(ident, section, state, owner, gifts_panel, stock_panel)
            elif action.startswith("itemmedia:"):
                image, caption, markup = await item_panel.media(action, state)
                await message.answer_photo(
                    BufferedInputFile(image, filename="item.jpg"), caption=caption, reply_markup=markup
                )
                return
            elif action.startswith("editfields:"):
                text, markup = await wizard.start_edit(owner, state, action.split(":", 1)[1])
            elif action.startswith("quote:"):
                ident = action.split(":", 1)[1]
                text, markup = await paid_promotions.callback("payp:item:" + ident, owner, state)
            elif action.startswith(("discount:", "editprice:")):
                ident = action.split(":", 1)[1]
                item = await products.owned_item(ident)
                if item.get("raw_price") is None:
                    raise ValidationError("API не вернул базовую цену. Изменение скидки недоступно.")
                await state.set_state("discount")
                await state.set_data({"item_id": ident})
                text, markup = (
                    f"💰 <b>Цена и скидка</b>\nБазовая цена: {item['raw_price']} ₽\nТекущая цена: {item['price']} ₽\n\nВведите цену со скидкой (минимум 5%). Для снятия скидки укажите базовую цену. Базовая цена не изменяется.",
                    keyboard([]),
                )
            elif action.startswith(("editphoto:", "ipe:")):
                text, markup = await item_panel.edit_photos(action, owner, state)
            elif action.startswith(("editdesc:", "editname:")):
                kind, ident = action.split(":", 1)
                item = await api.call("get_item", id=ident)
                if not api.account.id or (item.get("user") or {}).get("id") != api.account.id:
                    raise ValidationError("Владение товаром не подтверждено")
                await products.prepare_item_agreements(item)
                await state.set_state("editphoto" if kind == "editphoto" else "edit")
                await state.set_data(
                    {
                        "item_id": ident,
                        "field": {
                            "editprice": "price",
                            "editdesc": "description",
                            "editname": "name",
                            "editphoto": "photos",
                        }[kind],
                        "attachment_ids": [a.get("id") for a in item.get("attachments") or []],
                        "expected_description": item.get("description"),
                        "expected_name": item.get("name"),
                    }
                )
                shown_description = item.get("description") or ""
                if kind == "editname":
                    from app.services.logical_products import LogicalProducts, marker

                    product = LogicalProducts(store, api).listing(ident)
                    if product:
                        if len(product["code"]) != 3 or marker(item.get("name")) != product["code"]:
                            raise ValidationError(
                                "Постоянный код названия не подтверждён. Откройте привязку товара"
                            )
                        await state.update_data(title_code=product["code"])
                text, markup = (
                    "Пришлите одно новое изображение. После подтверждения оно заменит все текущие фотографии."
                    if kind == "editphoto"
                    else "Сейчас на Playerok:\n<blockquote>"
                    + escaped(shown_description if kind == "editdesc" else item.get("name"), 2800)
                    + "</blockquote>\nВведите новое значение. Затем будет подтверждение.",
                    keyboard([]),
                )
            elif action.startswith(("delete:", "publish:")):
                kind, ident = action.split(":", 1)
                pending = {"kind": kind, "target": ident, "payload": {}}
                if kind == "publish":
                    item = await api.call("get_item", id=ident)
                    base_price = priority_price_basis(item)
                    quote = await products.free_priority(ident, base_price)
                    pending["payload"] = {
                        "price": item["price"],
                        "raw_price": base_price,
                        "priority_id": quote["id"],
                    }
                nonce = secrets.token_hex(8)
                await state.set_data({"pending": pending, "confirm_token": nonce})
                text, markup = (
                    (
                        "Подтвердите удаление лота."
                        if kind == "delete"
                        else "API подтвердил бесплатный DEFAULT (0 ₽). Перед публикацией условия будут проверены повторно."
                    ),
                    keyboard([[("Подтвердить", "commit:" + nonce)]]),
                )
            elif action.startswith("commit:"):
                data = await state.get_data()
                if action.split(":", 1)[1] != data.get("confirm_token") or not data.get("pending"):
                    raise ValidationError("Подтверждение устарело или уже принято")
                if not api.account.writes_enabled:
                    raise ValidationError("Записи Playerok отключены в локальном .env")
                pending = data["pending"]
                if pending["kind"] in {"reply", "mark_read"}:
                    await state.clear()
                    raise ValidationError(
                        "Старое подтверждение больше не используется. Откройте переписку и нажмите «Ответить»"
                    )
                ident = Replies(store).enqueue(owner, "ui:" + data["confirm_token"], pending)
                store.audit(owner, "QUEUE_" + pending["kind"], pending["target"], "PENDING")
                await state.clear()
                text, markup = (
                    "⏳ Ожидает выполнения. Этот экран покажет подтверждённый результат; его также можно проверить кнопкой ниже.",
                    keyboard([[("Открыть результат", "result:show:" + ident)]]),
                )
            elif action in {"orders", "orderfilters"} or action.startswith(("orderpage:", "order:", "od:")):
                if action == "orders":
                    await state.update_data(
                        order_search=store.setting(f"ui:{owner}:order_search", ""),
                        order_filter=store.setting(f"ui:{owner}:order_filter", "ALL"),
                    )
                text, markup = await orders_panel.callback(action, owner, state)
            elif action.startswith(("chat:", "chatpage:")):
                text, markup = await chats.show(action, state)
            elif action.startswith("chatmedia:"):
                data, caption, markup = await chats.media(action, state)
                await message.answer_photo(
                    BufferedInputFile(data, filename="chat-image.jpg"), caption=caption, reply_markup=markup
                )
                return
            elif action == "sync":
                await synchronizer.once()
                text, markup = (
                    "Цикл сверки завершён. Статус: "
                    + escaped(synchronizer.health.get("status", "UNKNOWN"))
                    + ". Сохранённая история доступна локально; полная загрузка может занять несколько циклов.",
                    keyboard([[("🛒 Заказы", "orders")]]),
                )
            elif action in {"profile", "profile:check", "profile:details"}:
                return await profile_panel.handle(
                    message,
                    refresh=action == "profile:check" and not preserve,
                    details=action == "profile:details",
                )
            elif action.startswith(("stats:", "st:", "sd:", "sr:", "sg:")):
                return await statistics_panel.handle(action, message, owner=owner, state=state)
            elif action == "settings" or action.startswith("sp:"):
                if action == "settings":
                    await state.clear()
                text, markup = settings_panel.handle(action, owner)
            elif action == "backup":
                from datetime import datetime

                destination = Path("data/backups") / (
                    datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + ".sqlite3"
                )
                await asyncio.to_thread(store.backup, destination)
                text, markup = (
                    "Резервная копия сохранена локально в data/backups. В Telegram база с данными выдачи не отправляется.",
                    keyboard([]),
                )
            elif action == "diagnostics":
                health = store.setting("sync:health", {"status": "NOT_STARTED"})
                actions = store.rows(
                    "SELECT kind,status,error_code,created_at FROM outbox ORDER BY created_at DESC LIMIT 10"
                )
                text = (
                    "🩺 <b>Диагностика</b>\nСверка: "
                    + escape(str(health))
                    + "\nWebSocket: "
                    + escaped(store.setting("realtime:health", {"status": "NOT_STARTED"}), 400)
                    + "\nАдресная сверка: "
                    + escaped(store.setting("realtime:reconciliation", {}), 200)
                    + "\nПланировщик продвижения: "
                    + escaped(store.setting("promotions:health", {"status": "NOT_STARTED"}), 300)
                    + "\nАвтоматическая регистрация заказов: "
                    + escaped(store.setting("enrollment:health", {"status": "NOT_STARTED"}), 300)
                    + "\nТранзакции: "
                    + escaped(store.setting("transactions:health", {"status": "NOT_STARTED"}), 300)
                    + "\nАвтоответы: "
                    + escaped(store.setting("auto_replies:health", {"status": "NOT_STARTED"}), 200)
                    + "\nНастройки уведомлений: "
                    + escaped(store.setting(f"notifications:{owner}:preferences_error", "OK"), 100)
                    + "\n\n"
                    + "\n".join(
                        escape(f"{a['kind']}: {a['status']} · {a['error_code'] or ''}") for a in actions
                    )
                )
                proxy = active_proxy.get()
                if proxy is not None:
                    status = proxy.state
                    text += "\n\nPlayerok Proxy · SOCKS5 127.0.0.1:1080\n" + escaped(
                        {
                            key: status.get(key)
                            for key in ("state", "serverId", "lastCheck", "error", "telegramAvailable")
                        },
                        700,
                    )
                workers = store.rows(
                    "SELECT key,value FROM settings WHERE key LIKE 'runtime:worker:%' ORDER BY key"
                )
                if workers:
                    text += "\n\nФоновые задачи — последние сохранённые состояния (не проверка API):"
                    for worker in workers:
                        value = json.loads(worker["value"])
                        text += "\n" + escaped(
                            worker["key"].removeprefix("runtime:worker:")
                            + ": "
                            + str(value.get("state"))
                            + " · "
                            + local_time(value.get("updated_at"))
                            + " · сбоев: "
                            + str(value.get("failures_total", 0)),
                            180,
                        )
                text += "\n\nUNCERTAIN не повторяется автоматически. Для разрешения сверяйте результат на Playerok."
                markup = keyboard(
                    [
                        [("🔄 Сверить API", "sync")],
                        [("📄 Условия продавца · просмотр", "agv:list:0")],
                        [("⚠️ Неопределённые операции", "ops:list:UNCERTAIN:0")],
                        [("🧾 История операций", "ops:list:ALL:0")],
                    ]
                )
            else:
                raise ValidationError("Кнопка устарела. Откройте главное меню.")
            await display(message, text, markup, not preserve)
        except (APIError, ValidationError, Conflict, ValueError) as error:
            await message.answer("❗ " + error_text(error), reply_markup=keyboard([]))
        except sqlite3.Error:
            await message.answer(
                "❗ Не удалось сохранить локальные данные. Изменения транзакции отменены. Проверьте свободное место и откройте раздел заново.",
                reply_markup=keyboard([]),
            )

    @router.message(F.text | F.photo | F.document)
    async def input_message(message, state):
        if await state.get_state() == "chatcard:reply":
            cards = ChatCards(store, message.bot)
            data = await state.get_data()
            try:
                await cards.reply(message, state, api)
            except (ValueError, APIError) as error:
                store.execute(
                    "UPDATE chat_cards SET hint=? WHERE chat_id=? AND owner_id=? AND account_id=?",
                    (error_text(error), data.get("chat_id"), message.from_user.id, cards.account),
                )
                await cards.flush()
            return
        message, _ = await navigator.enter(message, message.from_user.id, state, "input")
        try:
            current = await state.get_state()
            if current == "wizard":
                text, markup = await wizard.message(message, state)
            elif current == "statsgroup:choose":
                text, markup = await statistics_panel.message(message, state)
            elif current and current.startswith("launch:"):
                text, markup = await launch_panel.message(message, state)
            elif current == "stock:input":
                text, markup = await stock_panel.message(message, state)
            elif current and current.startswith("logical:"):
                text, markup = await logical_panel.message(message, state)
            elif current and current.startswith("gifts:"):
                text, markup = await gifts_panel.message(message, state)
            elif current and current.startswith("finance:"):
                text, markup = await finance.message(message, state)
            elif current == "reply" or current and current.startswith("quick:"):
                await state.clear()
                text, markup = (
                    "Откройте переписку и нажмите «Ответить».",
                    keyboard([[("💬 Сообщения", "chats")]]),
                )
            elif current and current.startswith("itemlist:"):
                text, markup = await listings_panel.filter_message(message, state)
            elif current == "draftlist:search":
                text, markup = await drafts_panel.message(message, state)
            elif current and current.startswith("order:"):
                text, markup = await orders_panel.message(message, state)
            elif current and current.startswith("profit:"):
                text, markup = await order_finance_panel.message(message, state)
            elif current and current.startswith("platform:"):
                text, markup = await platform_panel.message(message, state)
            elif current and current.startswith("automation:"):
                text, markup = await automation_panel.message(message, state)
            elif current and current.startswith("autoreply:"):
                text, markup = await auto_replies_panel.message(message, state)
            elif current and current.startswith("promotion:"):
                text, markup = await promotions_panel.message(message, state)
            elif current and current.startswith("fulfill:"):
                text, markup = await fulfillment_panel.message(message, state)
            elif current and current.startswith("resolve:"):
                text, markup = await operations.message(message, state)
            elif current == "discount":
                data = await state.get_data()
                item = await products.owned_item(data["item_id"])
                plan = discount_plan(item, message.text or "", getattr(api.account, "role", None))
                nonce = secrets.token_hex(8)
                await state.set_state(None)
                await state.set_data(
                    {
                        "pending": {"kind": "discount", "target": data["item_id"], "payload": plan},
                        "confirm_token": nonce,
                    }
                )
                text = (
                    f"💰 <b>{'Снять скидку' if plan['restore'] else 'Установить скидку'}</b>\n"
                    f"Базовая цена: {plan['expected_raw_price']} ₽\nБыло: {plan['expected_price']} ₽\n"
                    f"Новая цена: {plan['price']} ₽\nСкидка: {plan['discount_percent']}%\n\n"
                    "Перед изменением условия будут проверены повторно."
                )
                markup = keyboard([[("✅ Подтвердить цену", "commit:" + nonce)]])
            elif current == "editphoto":
                data = await state.get_data()
                file = message.photo[-1] if message.photo else message.document
                if not file:
                    raise ValidationError("Пришлите фотографию или файл JPEG/PNG/WEBP")
                source = await wizard.attachments.download(message.bot, file)
                nonce = secrets.token_hex(8)
                pending = {
                    "kind": "edit",
                    "target": data["item_id"],
                    "payload": {"add_photos": [source], "replace_attachment_ids": data["attachment_ids"]},
                }
                if "keep_attachment_ids" in data:
                    pending["payload"]["keep_attachment_ids"] = data["keep_attachment_ids"]
                try:
                    await state.set_data({"pending": pending, "confirm_token": nonce})
                    await state.set_state(None)
                except BaseException:
                    wizard.attachments.discard(source)
                    raise
                await message.answer_photo(
                    BufferedInputFile(await wizard.attachments.read(source), filename="replacement.jpg"),
                    caption="Заменить ВСЕ фотографии?"
                    if data.get("photo_mode") == "all" or "keep_attachment_ids" not in data
                    else "Сохранить изменение? Остальные фотографии сохранятся.",
                    reply_markup=keyboard([[("✅ Подтвердить изменение фото", "commit:" + nonce)]]),
                )
                return
            elif current == "edit":
                data = await state.get_data()
                value = message.text or ""
                if not value.strip() or len(value) > 3500:
                    raise ValidationError("Введите текст длиной 1–3500 символов")
                if data["field"] == "price":
                    value = listing_price(value, getattr(api.account, "role", None))
                else:
                    value = listing_text(value, data["field"])
                pending = {"kind": "edit", "target": data["item_id"], "payload": {data["field"]: value}}
                if data["field"] == "name" and data.get("title_code"):
                    from app.services.product_codes import titled

                    value = titled(value, data["title_code"])
                    pending["payload"]["name"] = value
                if data["field"] == "name":
                    pending["payload"]["expected_name"] = data.get("expected_name")
                if data["field"] == "description":
                    pending["payload"]["expected_description"] = data["expected_description"]
                nonce = secrets.token_hex(8)
                await state.set_state(None)
                await state.set_data({"pending": pending, "confirm_token": nonce})
                text, markup = (
                    "Подтвердите действие:\n\n" + escape(str(value)),
                    keyboard([[("✅ Подтвердить", "commit:" + nonce)]]),
                )
            else:
                text, markup = "Выберите действие в меню.", main_menu()
            await display(message, text, markup)
        except (APIError, ValidationError, Conflict, ValueError) as error:
            await message.answer("❗ " + error_text(error))
        except sqlite3.Error:
            await message.answer(
                "❗ Не удалось сохранить локальные данные. Изменения транзакции отменены. Проверьте свободное место и откройте раздел заново.",
                reply_markup=keyboard([]),
            )

    return router
