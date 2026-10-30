"""Owner-facing durable receipts, using the existing notification loop."""

import asyncio
import json
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton

from app.database.store import Conflict
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard

TITLES = {
    "gift_send": "Telegram-подарок",
    "gift_refund": "Возврат подарочного заказа",
    "gift_notice": "Уведомление по подарку",
    "create": "Создание лота",
    "publish": "Публикация",
    "edit": "Изменение лота",
    "edit_fields": "Изменение характеристик",
    "discount": "Цена и скидка",
    "delete": "Удаление лота",
    "delivery": "Выдача товара",
    "delivery_text": "Отправка товара",
    "fulfill": "Подтверждение выполнения",
}


class OperationResults:
    def __init__(self, store):
        self.store = store

    def owner(self, owner):
        if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (owner,)):
            raise Conflict("Доступ закрыт")

    def show(self, owner, ident):
        self.owner(owner)
        rows = self.store.rows(
            "SELECT o.* FROM outbox o JOIN operation_receipts r ON r.operation_id=o.id WHERE o.id=? AND r.owner_id=?",
            (ident, owner),
        )
        if not rows:
            raise Conflict("Результат операции недоступен")
        row = rows[0]
        result = json.loads(row["result"] or "null") or {}
        status, kind = row["status"], row["kind"]
        title = TITLES.get(kind, "Операция")
        label = {
            "PENDING": "В очереди (QUEUED)",
            "PROCESSING": "Выполняется",
            "RETRYABLE_FAILED": "Ожидает безопасной обработки",
            "SUCCEEDED": "Подтверждено (SUCCEEDED)",
            "UNCERTAIN": "Результат неизвестен (UNCERTAIN)",
            "PERMANENT_FAILED": "Операция остановлена",
        }[status]
        text = f"🧾 <b>{title}</b>\n{label}\nОперация: <code>{escaped(ident, 100)}</code>"
        buttons = []
        if status in {"PENDING", "PROCESSING", "RETRYABLE_FAILED"}:
            text += "\nПостановка в очередь не означает выполнения."
        elif status == "UNCERTAIN" or row["error_code"] == "MANUALLY_STOPPED_UNCERTAIN":
            text += "\n⚠️ Запрос мог выполниться. Не повторяйте его. Сначала проверьте фактический результат."
            buttons.append([("Безопасная сверка", "ops:show:" + ident)])
        elif status == "PERMANENT_FAILED":
            code = row["error_code"] or ""
            reason = "Условия операции не прошли повторную проверку. Откройте объект и проверьте актуальные данные."
            if code in {"UNAUTHORIZED_OR_CHALLENGE", "SESSION_EXPIRED"}:
                reason = "Playerok не подтвердил сессию. Проверьте подключение; обход защиты не выполняется."
            if code.startswith("HTTP_"):
                reason = "Playerok отклонил запрос. Проверьте данные и фактическое состояние объекта перед новым действием."
            text += "\n" + reason
        if status == "SUCCEEDED":
            if kind in {"delivery", "delivery_text"}:
                text += "\nСервер подтвердил отправку сообщения. Это не подтверждение прочтения или пригодности товара."
                text += (
                    "\nСообщение: <code>" + escaped(result.get("message_id") or "не указан", 100) + "</code>"
                )
            elif kind == "delete":
                text += "\nСервер подтвердил удаление этого лота."
            else:
                if result.get("name"):
                    text += "\n" + escaped(result["name"], 150)
                if result.get("remote_id"):
                    text += "\nID: <code>" + escaped(result["remote_id"], 100) + "</code>"
                if result.get("status") or result.get("remote_status"):
                    actual = result.get("status") or result["remote_status"]
                    text += "\nСтатус в ответе сервера: " + escaped(actual, 60)
                    if actual in {"PENDING_APPROVAL", "PENDING_MODERATION"}:
                        text += "\nОжидает модерации; активным пока не считается."
                if result.get("price") is not None:
                    text += "\nЦена: " + escaped(result["price"], 40) + " ₽"
                base = result.get("raw_price")
                if base is not None:
                    text += "\nБазовая цена: " + escaped(base, 40) + " ₽"
        if kind.startswith("gift_"):
            job = self.store.rows(
                "SELECT state,refund_state FROM gift_jobs WHERE order_id=?", (row["target"],)
            )
            if job:
                text += f"\nПодарок: {job[0]['state']} · возврат: {job[0]['refund_state']}"
            buttons.append([("Подарочный заказ и безопасная сверка", "gft:job:" + row["target"])])
        elif kind in {"delivery", "delivery_text", "fulfill"}:
            buttons.append([("Заказ и состояние склада", "order:" + row["target"])])
            if status == "SUCCEEDED" and kind != "fulfill":
                buttons.append([("Выдано — ожидает подтверждения", "delivered:0")])
        elif kind == "create":
            binding = self.store.rows("SELECT remote_id FROM bindings WHERE draft_id=?", (row["target"],))
            if binding:
                target = binding[0]["remote_id"]
                buttons += [
                    [("📦 Лот и доступные действия", "item:" + target)],
                    [("📚 Склад / перенос из мастера", "stock:view:" + target)],
                ]
        elif kind != "delete":
            buttons.append([("Открыть актуальную карточку", "item:" + row["target"])])
        buttons += [
            [("🔄 Обновить результат", "result:show:" + ident)],
            [("Все результаты", "result:list:0")],
        ]
        markup = keyboard(buttons)
        if status == "SUCCEEDED" and kind in {"create", "publish", "edit", "discount"}:
            target = result.get("remote_id")
            if target:
                markup.inline_keyboard.insert(
                    0,
                    [
                        InlineKeyboardButton(
                            text="🔗 Открыть на Playerok",
                            url="https://playerok.com/products/"
                            + quote(str(result.get("slug") or target), safe=""),
                        )
                    ],
                )
        return text, markup

    def listing(self, owner, page):
        self.owner(owner)
        if not 0 <= page <= 100000:
            raise Conflict("Страница недоступна")
        rows = self.store.rows(
            "SELECT o.id,o.kind,o.status FROM operation_receipts r JOIN outbox o ON o.id=r.operation_id WHERE r.owner_id=? ORDER BY o.created_at DESC,o.id DESC LIMIT 11 OFFSET ?",
            (owner, page * 10),
        )
        buttons = [
            [(TITLES.get(row["kind"], row["kind"]) + " · " + row["status"], "result:show:" + row["id"])]
            for row in rows[:10]
        ]
        nav = []
        if page:
            nav.append(("← Назад", f"result:list:{page - 1}"))
        if len(rows) > 10:
            nav.append(("Далее →", f"result:list:{page + 1}"))
        if nav:
            buttons.append(nav)
        return "🧾 <b>Результаты моих операций</b>\n" + (
            "Выберите результат." if rows else "Операций нет."
        ), keyboard(buttons)

    async def notify(self, bot):
        rows = self.store.rows(
            "SELECT r.operation_id,r.owner_id FROM operation_receipts r JOIN outbox o ON o.id=r.operation_id WHERE r.notice_status='PENDING' AND o.status IN ('SUCCEEDED','UNCERTAIN','PERMANENT_FAILED') LIMIT 20"
        )
        for row in rows:
            # Persist admission before Telegram; a lost Telegram ACK must not resend.
            changed = self.store.execute(
                "UPDATE operation_receipts SET notice_status='PROCESSING' WHERE operation_id=? AND owner_id=? AND notice_status='PENDING'",
                (row["operation_id"], row["owner_id"]),
            ).rowcount
            if not changed:
                continue
            try:
                text, markup = self.show(row["owner_id"], row["operation_id"])
                message = await bot.send_message(row["owner_id"], text, reply_markup=markup)
            except asyncio.CancelledError:
                self.store.execute(
                    "UPDATE operation_receipts SET notice_status='UNCERTAIN' WHERE operation_id=? AND owner_id=?",
                    (row["operation_id"], row["owner_id"]),
                )
                raise
            except Exception:  # noqa: BLE001 -- no raw Telegram errors or buyer data
                self.store.execute(
                    "UPDATE operation_receipts SET notice_status='UNCERTAIN' WHERE operation_id=? AND owner_id=?",
                    (row["operation_id"], row["owner_id"]),
                )
            else:
                self.store.execute(
                    "UPDATE operation_receipts SET notice_status='SENT',telegram_message_id=? WHERE operation_id=? AND owner_id=?",
                    (message.message_id, row["operation_id"], row["owner_id"]),
                )
