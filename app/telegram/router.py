import asyncio
import json
import secrets
from datetime import UTC
from html import escape
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile

from app.database.store import Conflict
from app.playerok.adapter import APIError, safe_media_url
from app.rendering.cards import profile_card
from app.rendering.charts import revenue_chart
from app.services.discounts import discount_plan
from app.services.products import ValidationError, listing_price, listing_text, priority_price_basis
from app.services.statistics import statistics
from app.telegram.automation_panel import AutomationPanel
from app.telegram.chat_panel import ChatPanel
from app.telegram.finance_panel import FinancePanel
from app.telegram.formatting import escaped
from app.telegram.fulfillment_panel import FulfillmentPanel
from app.telegram.keyboards import keyboard, main_menu
from app.telegram.operations_panel import OperationsPanel
from app.telegram.orders_panel import OrdersPanel
from app.telegram.promotions_panel import PromotionsPanel
from app.telegram.replies_panel import RepliesPanel
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
    promotions_panel = PromotionsPanel(store, api)

    @router.message(CommandStart())
    async def start(message, state):
        await state.clear()
        await message.answer(
            "🟣 <b>PLAYEROK CONTROL CENTER</b>\n\nПанель магазина. Платные операции запрещены.\n"
            "Черновики и история сохраняются локально.",
            reply_markup=main_menu(),
        )

    async def listing(kind, state, cursor=None):
        if kind == "items":
            filters = (await state.get_data()).get("item_filters", {})
            status = filters.get("status")
            if status and status not in ItemStatuses.__members__:
                raise ValidationError("Неизвестный статус товара")
            page = await api.call(
                "get_my_items",
                count=10,
                after_cursor=cursor,
                statuses=[ItemStatuses[status]] if status else None,
            )
            values, prefix = page.get("items") or [], "item"
            title = "📦 Мои товары"
        elif kind == "chats":
            page = await api.call("get_chats", count=10, after_cursor=cursor)
            values, prefix = page.get("chats") or [], "chat"
            title = "💬 Диалоги"
        else:
            raise ValidationError("Неизвестный список")
        rows = []
        for value in values:
            user = value.get("user") or {}
            label = value.get("name") or user.get("username") or value.get("id")
            if kind == "items":
                label = f"{value.get('price', '?')} ₽ · {label}"
            elif value.get("unread_messages_counter"):
                label = f"🔵 {value['unread_messages_counter']} · {label}"
            rows.append([(str(label)[:60], f"{prefix}:{value['id']}")])
        info = page.get("page_info") or {}
        if info.get("has_next_page"):
            await state.update_data(list_kind=kind, list_next=info["end_cursor"])
            rows.append([("Далее →", f"listnext:{kind}")])
        if kind == "items":
            rows.append([("🔎 Статусы", "itemfilters")])
        return f"<b>{title}</b>\n" + ("Выберите запись." if values else "Записей нет."), keyboard(rows)

    @router.callback_query()
    async def callback(query, state):
        await query.answer()
        action = query.data or ""
        owner = query.from_user.id
        message = query.message
        preserve = bool(
            store.rows(
                "SELECT 1 FROM notifications WHERE owner_id=? AND telegram_message_id=? LIMIT 1",
                (owner, message.message_id),
            )
        )
        try:
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
            elif finance.handles(action):
                text, markup = await finance.callback(action, owner, state)
            elif action == "quickreplies":
                text, markup = await replies.menu(owner, state)
            elif action.startswith("quick:"):
                text, markup = await replies.callback(action, owner, state)
            elif action.startswith("ops:"):
                text, markup = await operations.callback(action, owner, state)
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
            elif action == "drafts":
                drafts = store.rows(
                    "SELECT id,payload,state FROM drafts WHERE owner_id=? ORDER BY created_at DESC LIMIT 30",
                    (owner,),
                )
                rows = [
                    [
                        (
                            (json.loads(d["payload"]).get("name") or "Новый товар")[:42] + " · " + d["state"],
                            "draft:" + d["id"],
                        )
                    ]
                    for d in drafts
                ]
                text, markup = "💾 <b>Черновики</b>\nПоследние 30 записей.", keyboard(rows)
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
                text, markup = await listing(action, state)
            elif action == "itemfilters":
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
                text, markup = "📦 Фильтр товаров по реальным статусам API", keyboard(rows)
            elif action.startswith("itemfilter:"):
                value = action.split(":", 1)[1]
                await state.set_data({"item_filters": {"status": None if value == "ALL" else value}})
                text, markup = await listing("items", state)
            elif action.startswith("listnext:"):
                kind = action.split(":", 1)[1]
                data = await state.get_data()
                if data.get("list_kind") != kind or not data.get("list_next"):
                    raise ValidationError("Список устарел")
                text, markup = await listing(kind, state, data["list_next"])
            elif action.startswith("item:"):
                ident = action.split(":", 1)[1]
                item = await api.call("get_item", id=ident)
                if (item.get("user") or {}).get("id") != api.account.id:
                    raise ValidationError("Лот не принадлежит вашему аккаунту")
                text = (
                    f"📦 <b>{escape(item.get('name') or ident)}</b>\nЦена: {item.get('price')} ₽\n"
                    f"Статус: {escape(item.get('status') or '?')}\nПриоритет: {escape(item.get('priority') or '?')}\n"
                    f"Позиция: {item.get('priority_position') or 'нет данных'}\n\n{escaped(item.get('description') or '', 1700)}\n\n"
                    f'<a href="https://playerok.com/products/{escape(item.get("slug") or ident, quote=True)}">Открыть на Playerok</a>'
                )
                rows = [
                    [("💰 Цена / Скидка", "discount:" + ident), ("📋 Описание", "editdesc:" + ident)],
                    [("📝 Название", "editname:" + ident), ("🖼 Заменить фото", "editphoto:" + ident)],
                    [("🧩 Атрибуты и данные", "editfields:" + ident)],
                    [("⭐ Стоимость размещения", "quote:" + ident)],
                    [("💰 Финансовый расчёт", "finance:" + ident)],
                    [("⚡ Политика выдачи · dry-run", "auto:item:" + ident)],
                    [("🚀 Продвижение · dry-run", "promo:item:" + ident)],
                    [("🗑 Удалить", "delete:" + ident)],
                ]
                if item.get("status") == "DRAFT":
                    rows.append([("Бесплатная публикация · превью", "publish:" + ident)])
                markup = keyboard(rows)
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
                    f"Операция сохранена: <code>{ident}</code>. Статус доступен в диагностике.",
                    keyboard([]),
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
            elif action == "profile":
                viewer = await api.call("get")
                profile = viewer.get("profile") or {}
                avatar = None
                url = profile.get("avatar_url")
                if url and safe_media_url(url):
                    import httpx

                    try:
                        async with (
                            httpx.AsyncClient(timeout=10, follow_redirects=False) as client,
                            client.stream("GET", url) as response,
                        ):
                            if response.status_code == 200:
                                chunks = bytearray()
                                async for chunk in response.aiter_bytes():
                                    chunks.extend(chunk)
                                    if len(chunks) > 5_000_000:
                                        break
                                if len(chunks) <= 5_000_000:
                                    avatar = bytes(chunks)
                    except httpx.HTTPError:
                        pass
                image = await asyncio.to_thread(profile_card, profile, avatar)
                balance = profile.get("balance") or {}
                caption = (
                    f"👤 <b>{escape(viewer.get('username') or '?')}</b>\n"
                    f"Рейтинг: {profile.get('rating', 'нет данных')} · Отзывов: {profile.get('reviews_count', 'нет данных')}\n"
                    f"Баланс: {balance.get('value', 'нет данных')} ₽\nДоступно к выводу: {balance.get('withdrawable', 'нет данных')} ₽\n"
                    f"Регистрация: {escape(viewer.get('created_at') or '?')}"
                )
                return await message.answer_photo(
                    BufferedInputFile(image, filename="profile.png"),
                    caption=caption,
                    reply_markup=keyboard([]),
                )
            elif action.startswith("stats:"):
                days = int(action.split(":")[1])
                stats = await asyncio.to_thread(statistics, store, days)
                image = await asyncio.to_thread(revenue_chart, stats)
                caption = (
                    f"📊 <b>Статистика · {'всё время' if days == 0 else str(days) + ' дн.'}</b>\n"
                    f"Заказов: {stats['orders']} · Оплаченных: {stats['paid']}\n"
                    f"Выручка до комиссий: {stats['revenue']} ₽\nПодтверждённые продажи: {stats['confirmed_revenue']} ₽\n"
                    f"Возвраты: {stats['refunds']} ₽\nСредний чек: {stats['average'] if stats['average'] is not None else 'нет данных'}\n"
                    f"Начало локального покрытия: {escape(str(stats['coverage_start'] or 'нет данных'))}\n"
                    "История ограничена доступными API данными. Комиссии, поступления и прибыль не подтверждены."
                )
                markup = keyboard(
                    [
                        [
                            ("24 ч", "stats:1"),
                            ("48 ч", "stats:2"),
                            ("7 дн", "stats:7"),
                            ("30 дн", "stats:30"),
                            ("Всё", "stats:0"),
                        ]
                    ]
                )
                return await message.answer_photo(
                    BufferedInputFile(image, filename="statistics.png"), caption=caption, reply_markup=markup
                )
            elif action == "settings":
                text = (
                    "⚙️ <b>Настройки</b>\nЗаписи API: "
                    + ("включены" if api.account.writes_enabled else "выключены")
                    + "\nПлатежи и автовывод запрещены.\nВладелец закреплён локально."
                )
                markup = keyboard(
                    [
                        [("🔐 Подключения", "diagnostics")],
                        [("💰 Условия вывода · просмотр", "providers")],
                        [("⚡ Быстрые ответы", "quickreplies")],
                        [("💾 Резервная копия БД", "backup")],
                        [("⚡ Автоматизация", "automation")],
                    ]
                )
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
                    + "\n\n"
                    + "\n".join(
                        escape(f"{a['kind']}: {a['status']} · {a['error_code'] or ''}") for a in actions
                    )
                )
                text += "\n\nUNCERTAIN не повторяется автоматически. Для разрешения сверяйте результат на Playerok."
                markup = keyboard(
                    [
                        [("🔄 Сверить API", "sync")],
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

    @router.message(F.text | F.photo | F.document)
    async def input_message(message, state):
        try:
            current = await state.get_state()
            if current == "wizard":
                text, markup = await wizard.message(message, state)
            elif current and current.startswith("finance:"):
                text, markup = await finance.message(message, state)
            elif current and current.startswith("quick:"):
                text, markup = await replies.message(message, state)
            elif current and current.startswith("order:"):
                text, markup = await orders_panel.message(message, state)
            elif current and current.startswith("automation:"):
                text, markup = await automation_panel.message(message, state)
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

    return router
