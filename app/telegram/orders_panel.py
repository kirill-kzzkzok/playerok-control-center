import json
import secrets
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton

from app.database.store import Conflict
from app.playerok.adapter import APIError
from app.services.automation import Automation
from app.services.catalog import ORDER_FILTERS, order_page
from app.services.finance import checked_time
from app.services.order_management import OrderManagement, deliverable, order_snapshot
from app.services.products import ValidationError
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.presentation import MODES, history_event, local_time, status


class OrdersPanel:
    def __init__(self, store, api):
        self.store, self.service = store, OrderManagement(store, api)

    def delivered(self, owner, page=0):
        self.service.owner(owner)
        if not 0 <= page <= 100000:
            raise Conflict("Страница недоступна")
        rows = self.store.rows(
            "SELECT o.id,o.payload FROM orders o WHERE o.status='PAID' AND (EXISTS("
            "SELECT 1 FROM outbox b WHERE b.target=o.id AND b.status='SUCCEEDED' AND (b.kind='delivery_text' OR "
            "(b.kind='delivery' AND EXISTS(SELECT 1 FROM reservations r JOIN inventory i ON i.id=r.inventory_id WHERE r.order_id=o.id AND i.status='DELIVERED')))) OR EXISTS(SELECT 1 FROM gift_jobs g WHERE g.order_id=o.id AND g.state='SUCCEEDED' AND g.receipt IS NOT NULL)) ORDER BY o.created_at DESC,o.id DESC LIMIT 11 OFFSET ?",
            (page * 10,),
        )
        buttons = []
        for row in rows[:10]:
            name = (json.loads(row["payload"]).get("item") or {}).get("name") or "Товар без названия"
            buttons.append([(str(name)[:45] + " · проверить выдачу", "order:" + row["id"])])
            buttons.append([("Подтверждение выполнения", "ff:start:" + row["id"])])
        nav = []
        if page:
            nav.append(("← Назад", f"delivered:{page - 1}"))
        if len(rows) > 10:
            nav.append(("Далее →", f"delivered:{page + 1}"))
        if nav:
            buttons.append(nav)
        return "📦 <b>Выдано — ожидает подтверждения</b>\n" + (
            "Отправка подтверждена. Проверьте результат и подтвердите выполнение на Playerok. Покупатель пока не подтвердил получение."
            if rows
            else "Таких заказов нет."
        ), keyboard(buttons)

    async def listing(self, state, page=0, selected="ALL"):
        data = await state.get_data()
        result = order_page(self.store, page, selected, data.get("order_search", ""))
        await state.set_state(None)
        await state.set_data({"order_filter": selected, "order_search": data.get("order_search", "")})
        text = f"🛒 <b>Заказы · {ORDER_FILTERS[selected][0]}</b>\nСтраница {page + 1} · Всего {result['total']}\nПоследний полный обход: {checked_time(self.store.setting('orders:last_complete_sync'))}"
        text += "\nСначала новые по серверной дате создания; неизвестные даты — в конце."
        if data.get("order_search"):
            text += "\nПоиск: " + escaped(data["order_search"], 200)
        rows = [
            [
                (
                    f"{value['amount'] or '?'} ₽ · {status(value['status'], 'order')} · {value.get('name') or 'Товар без названия'}"[
                        :60
                    ],
                    "order:" + value["id"],
                )
            ]
            for value in result["rows"]
        ]
        navigation = []
        if result["has_previous"]:
            navigation.append(("← Назад", f"orderpage:{page - 1}:{selected}"))
        if result["has_next"]:
            navigation.append(("Далее →", f"orderpage:{page + 1}:{selected}"))
        if navigation:
            rows.append(navigation)
        rows.extend(
            [
                [("🔎 Поиск", "od:search"), ("Фильтры", "orderfilters")],
                [("Все", "orderpage:0:ALL"), ("Оплаченные", "orderpage:0:PAID")],
                [("Требуют внимания", "orderpage:0:PROBLEM")],
                [("Сбросить поиск", "od:reset")],
            ]
        )
        return text, keyboard(rows, back=("⬅️ Мой магазин", "home"))

    async def card(self, owner, ident, *, details=False):
        cached = False
        try:
            order = await self.service.fresh(owner, ident)
        except APIError:
            self.service.owner(owner)
            rows = self.store.rows("SELECT payload FROM orders WHERE id=?", (ident,))
            if not rows:
                raise
            order, cached = json.loads(rows[0]["payload"]), True
        item, user = order.get("item") or {}, order.get("user") or {}
        annotation = self.service.local(ident)
        counts = self.store.rows(
            "SELECT status,COUNT(*) AS n FROM inventory WHERE item_id=? GROUP BY status", (item["id"],)
        )
        stock = {row["status"]: row["n"] for row in counts}
        operation = self.store.rows(
            "SELECT id,status FROM outbox WHERE operation_key=?", ("delivery:" + ident,)
        )
        gifts = self.store.rows("SELECT state,refund_state FROM gift_jobs WHERE order_id=?", (ident,))
        gift_listing = self.store.rows(
            "SELECT enabled FROM gift_listings WHERE item_id=? AND enabled=1", (item["id"],)
        )
        history_count = self.store.rows("SELECT COUNT(*) AS n FROM order_history WHERE order_id=?", (ident,))[
            0
        ]["n"]
        text = (
            f"🛒 <b>Заказ</b>\nID: <code>{escaped(ident, 100)}</code>\n"
            f"Покупатель: {escaped(user.get('username') or 'не указан', 100)}\n"
            f"Товар: {escaped(item.get('name') or item['id'], 250)}\nСумма: {escaped(item.get('price', '?'), 40)} ₽\n"
            f"Статус Playerok: {status(order['status'], 'order')}\nСоздан: {local_time(order.get('created_at'))}\n"
            f"Проблема Playerok: {'да' if order.get('has_problem') else 'нет' if order.get('has_problem') is False else 'не подтверждено'}\n"
            f"Локальная отметка: {'проблема' if annotation['problem'] else 'нет'}\n"
            f"{Automation(self.store, self.service.api).status_text(item['id'])}\n"
            f"Запас: {stock.get('AVAILABLE', 0)} доступно · {stock.get('RESERVED', 0)} в резерве · {stock.get('QUARANTINED', 0)} в карантине\n"
            f"Локальных событий: {history_count} · Событий Playerok: {len(order.get('logs') or [])}"
        )
        if cached:
            text += "\n\n⚠️ API недоступен. Показан локальный кеш; актуальность не подтверждена."
        if annotation["note"]:
            text += "\nЗаметка: " + escaped(annotation["note"], 500)
        if operation:
            text += "\nВыдача: " + status(operation[0]["status"])
        if gifts:
            text += f"\nПодарочная выдача: {status(gifts[0]['state'])} · возврат: {status(gifts[0]['refund_state'])}"
        text += "\n\nОтметку об отправке ставит продавец. Получение подтверждает покупатель."
        rows = []
        chat = (order.get("chat") or {}).get("id")
        if chat:
            rows.append([("💬 Диалог", "chat:" + chat), ("✉️ Написать", "compose:" + chat)])
        if not cached and order["status"] == "PAID":
            rows.append([("✅ Подтвердить выполнение", "ff:start:" + ident)])
            can_deliver = False
            try:
                deliverable(order, ident, self.service.api.account.id)
                can_deliver = stock.get("AVAILABLE", 0) > 0
            except ValidationError:
                pass
            if can_deliver and not operation and not gifts and not gift_listing:
                rows.append([("📦 Выдать из запаса", "od:prepare:" + ident)])
            if not gifts and not gift_listing:
                rows.append([("➕ Добавить уникальные коды", "od:stock:" + ident)])
        if gifts:
            rows.append([("🎁 Подарок / сверка / возврат", "gft:job:" + ident)])
        if operation:
            rows.append([("Операция выдачи", "ops:show:" + operation[0]["id"])])
        rows.extend(
            [
                [("📝 История", f"od:history:{ident}:0"), ("Заметка", "od:note:" + ident)],
                [("❗ Отметить проблему", "od:problem:" + ident)],
                [("🧾 Себестоимость и прибыль", "pf:card:" + ident)],
            ]
        )
        if annotation["problem"]:
            rows.append([("Снять локальную отметку", "od:clear:" + ident)])
        rows.append([("🔄 Проверить статус", "order:" + ident), ("🛒 К списку", "orders")])
        if not details:
            text = (
                "🛒 <b>"
                + escaped(item.get("name") or "Заказ", 200)
                + "</b>"
                + "\nСумма: "
                + escaped(item.get("price", "?"), 40)
                + " ₽"
                + "\nПокупатель: "
                + escaped(user.get("username") or "не указан", 80)
                + "\nСтатус: "
                + status(order["status"], "order")
            )
            if operation:
                text += "\nВыдача: " + status(operation[0]["status"])
            elif gifts:
                text += "\nПодарки: " + status(gifts[0]["state"])
            else:
                revision, policy = Automation(self.store, self.service.api).get(item["id"])
                text += "\nВыдача: " + (
                    "подарок ожидает обработки"
                    if gift_listing
                    else "вручную"
                    if revision and policy.mode == "manual"
                    else "способ ещё не подтверждён — ожидается сверка"
                )
            if not gifts and not gift_listing:
                readiness = Automation(self.store, self.service.api).readiness(item["id"])
                if readiness["mode"] != "manual":
                    text += "\nАвтовыдача: " + readiness["status"] + " · " + MODES[readiness["mode"]]
            if cached:
                text += "\n⚠️ Сохранённые данные. Связь с Playerok не подтверждена."
            if order.get("has_problem") or annotation["problem"]:
                text += "\n⚠️ Требует внимания — откройте подробности."
            # Keep availability from the existing guarded action list, never infer permission from UI state.
            rows = [
                [
                    button
                    for button in row
                    if (
                        button[1].startswith(("reply:", "compose:"))
                        or button[1].startswith("ff:start:")
                        or button[1].startswith("od:prepare:")
                    )
                ]
                for row in rows
            ]
            rows = [row for row in rows if row]
            rows += [
                [("📜 История заказа", "od:history:" + ident + ":0")],
                [("⋯ Подробнее", "od:details:" + ident)],
                [("⬅️ К заказам", "orders")],
            ]
            return text, keyboard(rows, home=False)
        markup = keyboard(rows)
        markup.inline_keyboard.insert(
            -1,
            [
                InlineKeyboardButton(
                    text="🔗 Открыть сделку Playerok",
                    url="https://playerok.com/deal/" + quote(ident, safe=""),
                )
            ],
        )
        return text, markup

    def history(self, owner, ident, page):
        self.service.owner(owner)
        if not 0 <= page <= 1_000_000:
            raise ValueError("Некорректная страница истории")
        rows = self.store.rows("SELECT * FROM orders WHERE id=?", (ident,))
        if not rows:
            raise Conflict("Заказ не найден")
        logs = sorted(
            json.loads(rows[0]["payload"]).get("logs") or [],
            key=lambda entry: entry.get("created_at") or "",
            reverse=True,
        )
        events = self.store.rows(
            "SELECT * FROM order_history WHERE order_id=? ORDER BY id DESC LIMIT 7 OFFSET ?",
            (ident, page * 6),
        )
        blocks = ["📜 <b>История заказа</b>"]
        for row in events[:6]:
            event = dict(row)
            block = f"{local_time(event['created_at'])} — {history_event(event)}"
            if event["source"] == "LOCAL_NOTE" and event["note"]:
                block += "\n" + escaped(event["note"], 220)
            if event["problem"]:
                block += (
                    "\n⚠️ Проблема (локально): да — отметка продавца"
                    if event["source"] == "LOCAL_NOTE"
                    else "\n⚠️ Площадка отметила проблему с заказом"
                )
            blocks.append(block)
        if logs[page * 6 : (page + 1) * 6]:
            blocks.append("<b>События площадки</b>")
        for entry in logs[page * 6 : (page + 1) * 6]:
            # The event enum has no stable public translation contract. Do not invent an action.
            blocks.append(f"{local_time(entry.get('created_at'))} — Обновлены данные заказа")
        if len(blocks) == 1:
            blocks.append("История пока пуста.")
        navigation = []
        if page:
            navigation.append(("← Назад", f"od:history:{ident}:{page - 1}"))
        if len(events) > 6 or len(logs) > (page + 1) * 6:
            navigation.append(("Далее →", f"od:history:{ident}:{page + 1}"))
        details = [
            [("Заметка целиком · " + str(event["id"]), "od:entry:" + str(event["id"]))]
            for event in events[:6]
            if len(event["note"]) > 150
        ]
        return "\n\n".join(blocks), keyboard(
            ([navigation] if navigation else [])
            + details
            + [[("Технический журнал", f"od:historyraw:{ident}:{page}")], [("К заказу", "order:" + ident)]]
        )

    def history_raw(self, owner, ident, page):
        self.service.owner(owner)
        if not 0 <= page <= 1_000_000:
            raise ValueError("Некорректная страница истории")
        values = self.store.rows(
            "SELECT source,old_status,new_status,created_at FROM order_history WHERE order_id=? ORDER BY id DESC LIMIT 6 OFFSET ?",
            (ident, page * 6),
        )
        text = "🛠 <b>Технический журнал заказа</b>\nID: <code>" + escaped(ident, 100) + "</code>"
        for value in values:
            text += "\n\n" + local_time(value["created_at"]) + " · источник: " + escaped(value["source"], 60)
            text += (
                "\nСостояние: "
                + escaped(value["old_status"] or "—", 60)
                + " → "
                + escaped(value["new_status"], 60)
            )
        order = self.store.rows("SELECT payload FROM orders WHERE id=?", (ident,))
        logs = json.loads(order[0]["payload"]).get("logs") or [] if order else []
        for entry in logs[page * 6 : (page + 1) * 6]:
            text += "\n\nСобытие площадки: " + escaped(entry.get("event") or "не указано", 120)
        return text, keyboard([], back=("⬅️ История заказа", f"od:history:{ident}:{page}"))

    async def confirm(self, state, pending, text):
        nonce = secrets.token_hex(8)
        await state.set_state(None)
        await state.set_data({"order_pending": pending, "order_token": nonce})
        return text, keyboard([[("✅ Подтвердить", "od:apply:" + nonce)]])

    async def callback(self, action, owner, state):
        self.service.owner(owner)
        if action == "orders":
            data = await state.get_data()
            return await self.listing(state, selected=data.get("order_filter", "ALL"))
        if action == "od:reset":
            await state.clear()
            return await self.listing(state)
        if action.startswith("orderpage:"):
            _, page, selected = action.split(":")
            return await self.listing(state, int(page), selected)
        if action == "orderfilters":
            return "Фильтры заказов", keyboard(
                [[(label, "orderpage:0:" + name)] for name, (label, _, _) in ORDER_FILTERS.items()]
            )
        if action.startswith("order:"):
            await state.clear()
            return await self.card(owner, action.split(":", 1)[1])
        parts = action.split(":")
        if (
            len(parts) < 2
            or (parts[1] != "search" and len(parts) < 3)
            or (parts[1] == "history" and len(parts) != 4)
        ):
            raise ValidationError("Кнопка заказа устарела")
        command = parts[1]
        if command == "search":
            selected = (await state.get_data()).get("order_filter", "ALL")
            await state.set_state("order:search")
            await state.set_data({"order_filter": selected})
            return "Введите ID сделки, название товара или username покупателя (до 200 символов).", keyboard(
                []
            )
        ident = parts[2]
        if command == "historyraw":
            if len(parts) != 4:
                raise ValidationError("Кнопка истории устарела")
            return self.history_raw(owner, ident, int(parts[3]))
        if command == "details":
            await state.clear()
            return await self.card(owner, ident, details=True)
        if command == "entry":
            rows = self.store.rows("SELECT order_id,note FROM order_history WHERE id=?", (int(ident),))
            if not rows:
                raise Conflict("Запись не найдена")
            return escaped(rows[0]["note"], 6000), keyboard([[("К заказу", "order:" + rows[0]["order_id"])]])
        if command == "history":
            return self.history(owner, ident, int(parts[3]))
        if command == "apply":
            data = await state.get_data()
            if ident != data.get("order_token") or not data.get("order_pending"):
                raise Conflict("Подтверждение устарело")
            pending = data["order_pending"]
            target = pending["id"]
            if pending["kind"] == "delivery":
                operation = await self.service.queue_delivery(owner, target, pending["snapshot"])
                result = (
                    "Операция выдачи: "
                    + operation
                    + ". Повторная отправка не создаётся; результат проверьте в истории операций."
                )
            elif pending["kind"] == "stock":
                count = await self.service.add_stock(owner, target, pending["snapshot"], pending["text"])
                result = (
                    f"Добавлено новых уникальных единиц: {count}. Уже известные коды не добавляются повторно."
                )
            else:
                self.service.orders.annotate(
                    target, owner, pending["revision"], pending["problem"], pending["note"]
                )
                result = "Локальная отметка сохранена. Проблема в Playerok не создавалась и не снималась."
            await state.clear()
            return result, keyboard([[("К заказу", "order:" + target)]])
        if command in {"prepare", "stock"}:
            order = await (
                self.service.prepare_delivery(owner, ident)
                if command == "prepare"
                else self.service.fresh(owner, ident)
            )
            pending = {
                "id": ident,
                "snapshot": order_snapshot(order),
                "kind": "delivery" if command == "prepare" else "stock",
            }
            if command == "prepare":
                return await self.confirm(
                    state,
                    pending,
                    "Выдать одну уникальную единицу из запаса в чат покупателя? Перед отправкой статус продажи, предмет, владелец, режим выдачи и доступность чата будут проверены ещё раз. Это не завершит сделку и не отметит её SENT.",
                )
            await state.set_state("order:stock")
            await state.set_data({"order_pending": pending})
            return (
                "Пришлите уникальные коды по одному на строку. Они сохранятся локально в запасе этого товара; отправки покупателю пока не будет.",
                keyboard([]),
            )
        if command in {"note", "problem", "clear"}:
            if not self.store.rows("SELECT 1 FROM orders WHERE id=?", (ident,)):
                raise Conflict("Сначала откройте карточку заказа")
            annotation = self.service.local(ident)
            pending = {
                "id": ident,
                "kind": "annotation",
                "revision": annotation["revision"],
                "problem": bool(annotation["problem"]) if command == "note" else command == "problem",
            }
            if command == "clear":
                pending["note"] = "Снята локальная отметка проблемы"
                return await self.confirm(
                    state,
                    pending,
                    "Снять только локальную отметку? Состояние проблемы в Playerok не изменится.",
                )
            await state.set_state("order:note")
            await state.set_data({"order_pending": pending})
            return (
                "Опишите локальную проблему или заметку (1–1000 символов). Не пишите секреты и данные выдачи. Жалоба в Playerok этим действием не отправляется.",
                keyboard([]),
            )
        raise ValidationError("Действие заказа недоступно")

    async def message(self, message, state):
        self.service.owner(message.from_user.id)
        current, data = await state.get_state(), await state.get_data()
        text = message.text or ""
        if current == "order:search":
            if not text.strip() or len(text) > 200:
                raise ValueError("Поиск: от 1 до 200 символов")
            await state.set_state(None)
            await state.update_data(order_search=text.strip())
            return await self.listing(state, selected=data.get("order_filter", "ALL"))
        pending = data["order_pending"]
        if current == "order:stock":
            if not text.strip() or len(text) > 10000:
                raise ValueError("Коды: от 1 до 10000 символов")
            pending["text"] = text
            return await self.confirm(
                state,
                pending,
                f"Добавить в локальный запас строк: {len([line for line in text.splitlines() if line.strip()])}? Значения не пересылаются покупателю до отдельной выдачи.",
            )
        if not text.strip() or len(text) > 1000:
            raise ValueError("Заметка: от 1 до 1000 символов")
        pending["note"] = text.strip()
        return await self.confirm(state, pending, "Сохранить локальную отметку?\n\n" + escaped(text, 1200))
