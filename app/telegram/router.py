import asyncio
import json
import secrets
import sqlite3
from datetime import UTC
from html import escape
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile

from app.database.store import Conflict
from app.localvpn import active_proxy
from app.playerok.adapter import APIError
from app.services.automation import Automation
from app.services.discounts import discount_plan
from app.services.products import ValidationError, listing_price, listing_text, priority_price_basis
from app.telegram.agreements_panel import AgreementsPanel
from app.telegram.auto_replies_panel import AutoRepliesPanel
from app.telegram.automation_panel import AutomationPanel
from app.telegram.automation_queue import AutomationQueue
from app.telegram.callback_ack import ACTION_MAX_WAIT, acknowledge, navigation
from app.telegram.chat_panel import ChatPanel
from app.telegram.drafts_panel import DraftsPanel
from app.telegram.finance_panel import FinancePanel
from app.telegram.formatting import escaped
from app.telegram.fulfillment_panel import FulfillmentPanel
from app.telegram.gifts_panel import GiftsPanel
from app.telegram.interface_cleanup_panel import InterfaceCleanupPanel
from app.telegram.item_panel import ItemPanel
from app.telegram.keyboards import keyboard, main_menu
from app.telegram.listings_panel import ListingsPanel
from app.telegram.operation_results import OperationResults
from app.telegram.operations_panel import OperationsPanel
from app.telegram.order_finance_panel import OrderFinancePanel
from app.telegram.orders_panel import OrdersPanel
from app.telegram.profile_panel import ProfilePanel
from app.telegram.promotions_panel import PromotionsPanel
from app.telegram.replies_panel import RepliesPanel
from app.telegram.settings_panel import SettingsPanel
from app.telegram.statistics_panel import StatisticsPanel
from app.telegram.stock_panel import StockPanel
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
    wizard = Wizard(store, api, products)
    finance = FinancePanel(store, api, products)
    chats = ChatPanel(store, api)
    replies = RepliesPanel(store)
    operations = OperationsPanel(store, api)
    orders_panel = OrdersPanel(store, api)
    fulfillment_panel = FulfillmentPanel(store, api)
    automation_panel = AutomationPanel(store, api)
    auto_replies_panel = AutoRepliesPanel(store, api)
    promotions_panel = PromotionsPanel(store, api)
    automation_queue = AutomationQueue(store, api)
    statistics_panel = StatisticsPanel(store)
    settings_panel = SettingsPanel(store, api)
    drafts_panel = DraftsPanel(store)
    interface_cleanup = InterfaceCleanupPanel(store)
    order_finance_panel = OrderFinancePanel(store, api)
    profile_panel = ProfilePanel(api, store)
    item_panel = ItemPanel(api)
    listings_panel = ListingsPanel(api)
    stock_panel = StockPanel(store, api)
    gifts_panel = GiftsPanel(store, api)
    operation_results = OperationResults(store)
    agreements_panel = AgreementsPanel(store, api, products)

    @router.message(CommandStart())
    async def start(message, state):
        await state.clear()
        await message.answer(
            "🟣 <b>PLAYEROK CONTROL CENTER</b>\n\nПанель магазина. Платежи за подарки требуют отдельных разрешений.\n"
            "Черновики и история сохраняются локально.",
            reply_markup=main_menu(),
        )

    @router.callback_query()
    async def callback(query, state, callback_ack=None):
        import time

        ack = callback_ack if callback_ack is not None else await acknowledge(query)
        action = query.data or ""
        owner = query.from_user.id
        message = query.message
        if message is None or not hasattr(message, "answer"):
            return
        if not navigation(action) and (
            ack["status"] != "ok" or time.monotonic() - ack["checked_at"] > ACTION_MAX_WAIT
        ):
            return await message.answer(
                "Нажатие не подтверждено или ожидало слишком долго. Действие не выполнено. "
                "Откройте экран заново; для ранее запущенной операции сначала проверьте её результат.",
                reply_markup=main_menu(),
            )
        preserve = bool(
            store.rows(
                "SELECT 1 FROM notifications WHERE owner_id=? AND telegram_message_id=? "
                "UNION ALL SELECT 1 FROM operation_receipts WHERE owner_id=? AND telegram_message_id=? LIMIT 1",
                (owner, message.message_id, owner, message.message_id),
            )
        )
        try:
            if action in {"profile", "profile:check", "diagnostics", "backup"} or action.startswith(
                (
                    "item:",
                    "order:",
                    "chat:",
                    "result:",
                    "delivered:",
                    "stats:",
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
                    "gft:list:",
                    "gft:catalog",
                    "gft:bindings",
                    "gft:reconcile:",
                    "gft:refundcheck:",
                )
            ):
                await state.clear()
            if action == "home":
                await state.clear()
                return await display(
                    message,
                    "🟣 <b>PLAYEROK CONTROL CENTER</b>\nУправление магазином",
                    main_menu(),
                    not preserve,
                )
            if action == "create":
                text, markup = await wizard.start(owner, state)
            elif action.startswith("agv:"):
                text, markup = await agreements_panel.callback(action, state)
            elif action.startswith("gft:"):
                text, markup = await gifts_panel.callback(action, owner, state)
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
            elif action == "quickreplies":
                text, markup = await replies.menu(owner, state)
            elif action.startswith("quick:"):
                text, markup = await replies.callback(action, owner, state)
            elif action.startswith("ops:"):
                text, markup = await operations.callback(action, owner, state)
            elif action.startswith("aq:"):
                text, markup = await automation_queue.callback(action, owner, state)
            elif action.startswith("ar:"):
                text, markup = await auto_replies_panel.callback(action, owner, state, message)
            elif action == "promotions" or action.startswith(("promo:", "pp:", "pi:")):
                text, markup = await promotions_panel.callback(action, owner, state)
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
                if draft["state"] == "DRAFT":
                    await state.set_state("wizard")
                    await state.set_data({"draft_id": ident, "revision": draft["revision"]})
                    text, markup = await wizard.screen(owner, state)
                else:
                    binding = store.rows("SELECT remote_id FROM bindings WHERE draft_id=?", (ident,))
                    if not binding and draft["payload"].get("edit_target"):
                        binding = [{"remote_id": draft["payload"]["edit_target"]}]
                    rows = [[("Открыть серверный лот", "item:" + binding[0]["remote_id"])]] if binding else []
                    text, markup = "Состояние: " + escape(draft["state"]), keyboard(rows)
            elif action in {"items", "chats"}:
                await state.clear()
                text, markup = await listings_panel.show(action, state)
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
                        [("Сбросить фильтры", "il:reset"), ("К списку", "il:back")],
                    ]
                )
                text, markup = (
                    "📦 Фильтры товаров\nСтатус и категория применяются сервером; название, цена и приоритет — на каждой загруженной странице.",
                    keyboard(rows),
                )
            elif action.startswith("itemfilter:"):
                value = action.split(":", 1)[1]
                if value != "ALL" and value not in ItemStatuses.__members__:
                    raise ValidationError("Неизвестный статус товара")
                filters = (await state.get_data()).get("item_filters", {})
                filters["status"] = None if value == "ALL" else value
                await state.set_data({"item_filters": filters})
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
            elif action.startswith("item:"):
                ident = action.split(":", 1)[1]
                item, text, photo_rows = await item_panel.card(ident, state)
                text += "\n\n" + (
                    gifts_panel.summary(ident)
                    if gifts_panel.service.listing(ident)
                    else escaped(Automation(store, api).status_text(ident), 1600)
                )
                rows = [
                    [("📚 Склад", "stock:view:" + ident)],
                    [("🎁 Telegram-подарок", "gft:item:" + ident)],
                    [("💰 Цена / Скидка", "discount:" + ident), ("📋 Описание", "editdesc:" + ident)],
                    [("📝 Название", "editname:" + ident), ("🖼 Заменить фото", "editphoto:" + ident)],
                    [("🧩 Атрибуты и данные", "editfields:" + ident)],
                    [("⭐ Стоимость размещения", "quote:" + ident)],
                    [("💰 Финансовый расчёт", "finance:" + ident)],
                    [("⚡ Политика выдачи · dry-run", "auto:item:" + ident)],
                    [("🚀 Продвижение · dry-run", "promo:item:" + ident)],
                    [("🗑 Удалить", "delete:" + ident)],
                ]
                for draft_id in stock_panel.service.prepared(owner, ident):
                    rows.append([("📥 Перенести запас из мастера", "stock:transfer:" + draft_id)])
                if item.get("status") == "DRAFT":
                    rows.append([("Бесплатная публикация · превью", "publish:" + ident)])
                markup = keyboard(photo_rows + rows)
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
                item = await api.call("get_item", id=ident)
                statuses = await products.priority_preview(ident, priority_price_basis(item))
                text = "⭐ <b>Стоимость из Playerok API</b>\n" + "\n".join(
                    f"{escape(s.get('name') or s.get('type') or '?')}: {s.get('price', 'неизвестно')} ₽ · {s.get('period', '?')} дн."
                    for s in statuses
                )
                text += "\n\nПросмотр ничего не покупает. Платные действия запрещены."
                markup = keyboard([[("⬅️ Лот", "item:" + ident)]])
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
            elif action.startswith(("editdesc:", "editname:", "editphoto:")):
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
                    }
                )
                text, markup = (
                    "Пришлите одно новое изображение. После подтверждения оно заменит все текущие фотографии."
                    if kind == "editphoto"
                    else "Введите новое значение. Затем будет подтверждение.",
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
                ident = replies.replies.enqueue(owner, "ui:" + data["confirm_token"], pending)
                store.audit(owner, "QUEUE_" + pending["kind"], pending["target"], "PENDING")
                await state.clear()
                text, markup = (
                    f"Операция в очереди (QUEUED): <code>{ident}</code>. Это ещё не результат; он придёт отдельным сообщением.",
                    keyboard([[("Открыть результат", "result:show:" + ident)]]),
                )
            elif action in {"orders", "orderfilters"} or action.startswith(("orderpage:", "order:", "od:")):
                text, markup = await orders_panel.callback(action, owner, state)
            elif action.startswith(("chat:", "chatpage:")):
                text, markup = await chats.show(action, state)
            elif action.startswith("chatmedia:"):
                data, caption, markup = await chats.media(action, state)
                await message.answer_photo(
                    BufferedInputFile(data, filename="chat-image.jpg"), caption=caption, reply_markup=markup
                )
                return
            elif action.startswith("readchat:"):
                ident = action.split(":", 1)[1]
                await api.call("get_chat", chat_id=ident)
                nonce = secrets.token_hex(8)
                await state.set_state(None)
                await state.set_data(
                    {
                        "pending": {
                            "kind": "mark_read",
                            "target": ident,
                            "payload": {},
                            "origin_event": replies.replies.origin(owner, message.message_id, ident),
                        },
                        "confirm_token": nonce,
                    }
                )
                text, markup = (
                    "Отметить все сообщения этого диалога прочитанными в Playerok?",
                    keyboard([[("✅ Подтвердить", "commit:" + nonce)]]),
                )
            elif action.startswith("reply:"):
                ident = action.split(":", 1)[1]
                await api.call("get_chat", chat_id=ident)
                origin = replies.replies.origin(owner, message.message_id, ident)
                text, markup = await replies.menu(owner, state, chat_id=ident, origin=origin)
            elif action == "sync":
                await synchronizer.once()
                text, markup = (
                    "Цикл сверки завершён. Статус: "
                    + escaped(synchronizer.health.get("status", "UNKNOWN"))
                    + ". Сохранённая история доступна локально; полная загрузка может занять несколько циклов.",
                    keyboard([[("🛒 Заказы", "orders")]]),
                )
            elif action in {"profile", "profile:check"}:
                return await profile_panel.handle(message, refresh=action == "profile:check")
            elif action.startswith(("stats:", "st:", "sd:")):
                return await statistics_panel.handle(action, message, owner=owner)
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
                            + str(value.get("updated_at"))
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
            code = error.code if isinstance(error, APIError) else str(error)
            await message.answer("❗ " + escaped(code, 800), reply_markup=keyboard([]))
        except sqlite3.Error:
            await message.answer(
                "❗ Не удалось сохранить локальные данные. Изменения транзакции отменены. Проверьте свободное место и откройте раздел заново.",
                reply_markup=keyboard([]),
            )

    @router.message(F.text | F.photo | F.document)
    async def input_message(message, state):
        try:
            current = await state.get_state()
            if current == "wizard":
                text, markup = await wizard.message(message, state)
            elif current == "stock:input":
                text, markup = await stock_panel.message(message, state)
            elif current and current.startswith("gifts:"):
                text, markup = await gifts_panel.message(message, state)
            elif current and current.startswith("finance:"):
                text, markup = await finance.message(message, state)
            elif current and current.startswith("quick:"):
                text, markup = await replies.message(message, state)
            elif current and current.startswith("itemlist:"):
                text, markup = await listings_panel.filter_message(message, state)
            elif current == "draftlist:search":
                text, markup = await drafts_panel.message(message, state)
            elif current and current.startswith("order:"):
                text, markup = await orders_panel.message(message, state)
            elif current and current.startswith("profit:"):
                text, markup = await order_finance_panel.message(message, state)
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
            elif current == "reply" and (message.photo or message.document):
                text, markup = await replies.photo(message, state)
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
                try:
                    await state.set_data({"pending": pending, "confirm_token": nonce})
                    await state.set_state(None)
                except BaseException:
                    wizard.attachments.discard(source)
                    raise
                await message.answer_photo(
                    BufferedInputFile(await wizard.attachments.read(source), filename="replacement.jpg"),
                    caption="Новое фото заменит все текущие фотографии после подтверждения.",
                    reply_markup=keyboard([[("✅ Заменить фотографии", "commit:" + nonce)]]),
                )
                return
            elif current in {"reply", "edit"}:
                data = await state.get_data()
                value = message.text or ""
                if not value.strip() or len(value) > 3500:
                    raise ValidationError("Введите текст длиной 1–3500 символов")
                if current == "reply":
                    text, markup = await replies.confirm(
                        state, data["chat_id"], value, data.get("origin_event")
                    )
                    return await display(message, text, markup)
                else:
                    if data["field"] == "price":
                        value = listing_price(value, getattr(api.account, "role", None))
                    else:
                        value = listing_text(value, data["field"])
                    pending = {"kind": "edit", "target": data["item_id"], "payload": {data["field"]: value}}
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
            await message.answer(
                "❗ " + escaped(error.code if isinstance(error, APIError) else str(error), 800)
            )
        except sqlite3.Error:
            await message.answer(
                "❗ Не удалось сохранить локальные данные. Изменения транзакции отменены. Проверьте свободное место и откройте раздел заново.",
                reply_markup=keyboard([]),
            )

    return router
