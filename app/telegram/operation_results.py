"""Owner-facing durable receipts, using the existing notification loop."""

import asyncio
import json
import time
from urllib.parse import quote

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton

from app.database.store import Conflict
from app.gifts.core import gift_name
from app.telegram.errors import TERMINAL_EDIT, send_failure, telegram_error
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.presentation import status as human_status

TITLES = {
    "reply": "Ответ покупателю",
    "mark_read": "Отметка прочитанности",
    "gift_send": "Telegram-подарок",
    "gift_refund": "Возврат подарочного заказа",
    "gift_notice": "Уведомление по подарку",
    "create": "Создание лота",
    "publish": "Публикация",
    "premium_publish": "Публикация PREMIUM",
    "premium_bump": "Платное поднятие PREMIUM",
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
            "PENDING": "Ожидает выполнения",
            "PROCESSING": "Выполняется",
            "RETRYABLE_FAILED": "Ожидает безопасной обработки",
            "SUCCEEDED": "Выполнено и подтверждено",
            "UNCERTAIN": "Результат неизвестен",
            "PERMANENT_FAILED": "Операция остановлена",
        }[status]
        text = f"🧾 <b>{title}</b>\n{label}"
        receipt = self.store.rows(
            "SELECT notice_status FROM operation_receipts WHERE operation_id=? AND owner_id=?", (ident, owner)
        )[0]
        if receipt["notice_status"] == "UNCERTAIN":
            text += "\nУведомление о результате могло прийти без подтверждения Telegram. Здесь показан сохранённый исход; повтор операции не выполняется."
        elif receipt["notice_status"] == "REJECTED":
            text += "\nTelegram отклонил уведомление. Результат операции сохранён здесь; сама операция не повторяется."
        buttons = []
        payload = json.loads(row["payload"])
        targets = self.store.rows("SELECT item_id FROM orders WHERE id=?", (row["target"],))
        item_id = (
            targets[0]["item_id"]
            if targets
            else row["target"]
            if self.store.rows("SELECT 1 FROM logical_listings WHERE item_id=?", (row["target"],))
            else None
        )
        if item_id:
            buttons.append([("🔍 Проверить готовность товара", "rd:check:" + item_id)])
        if status == "SUCCEEDED" and payload.get("platform_settings"):
            product = self.store.rows(
                "SELECT p.code,p.state,p.enabled FROM logical_products p JOIN logical_listings l ON l.product_id=p.id WHERE l.item_id=?",
                (row["target"],),
            )
            policies = self.store.rows(
                "SELECT payload FROM delivery_policies WHERE item_id=?", (row["target"],)
            )
            if product and product[0]["state"] == "VERIFIED":
                policy = json.loads(policies[0]["payload"]) if policies else {}
                active = product[0]["enabled"] and policy.get("enabled") and policy.get("mode") == "playerok"
                text = (
                    "✅ Привязка подтверждена\n🔐 Постоянный код: "
                    + escaped(product[0]["code"], 20)
                    + "\n📦 Выдача: Playerok"
                )
                text += "\n💬 Автоответ: " + (
                    "включён" if active and policy.get("paid_reply") else "выключен"
                )
                text += "\n✅ Автовыполнение: " + (
                    "включено" if active and policy.get("mark_sent") else "выключено"
                )
                if not policies:
                    text += "\n⚠️ Настройки не применены. Откройте товар и сохраните их заново."
                buttons.append([("Настройки выдачи", "platform:item:" + row["target"])])
        if status == "SUCCEEDED" and payload.get("native_relist"):
            text += "\nШтатное повторное размещение подтверждено. Копия товара не создавалась."
            gift = self.store.rows("SELECT 1 FROM gift_listings WHERE item_id=?", (row["target"],))
            text += "\n🎁 Telegram Gifts: " + ("привязка сохранена" if gift else "не настроено")
            text += "\n⚡ Готовность автовыдачи: проверьте актуальную карточку товара."
        if status in {"PENDING", "PROCESSING", "RETRYABLE_FAILED"}:
            text += "\nПостановка в очередь не означает выполнения."
        elif status == "UNCERTAIN" or row["error_code"] == "MANUALLY_STOPPED_UNCERTAIN":
            text += "\n⚠️ Запрос мог выполниться. Не повторяйте его. Сначала проверьте фактический результат."
            buttons.append(
                [
                    (
                        "🔍 Проверить результат на Playerok",
                        "ops:confirm:" + ident if kind == "edit" else "ops:show:" + ident,
                    )
                ]
            )
        elif status == "PERMANENT_FAILED":
            code = row["error_code"] or ""
            reason = "Условия операции не прошли повторную проверку. Откройте объект и проверьте актуальные данные."
            if code in {"UNAUTHORIZED_OR_CHALLENGE", "SESSION_EXPIRED"}:
                reason = "Playerok не подтвердил сессию. Проверьте подключение; обход защиты не выполняется."
            if code.startswith("HTTP_"):
                reason = "Playerok отклонил запрос. Проверьте данные и фактическое состояние объекта перед новым действием."
            from app.services.platform_orders import REASONS

            reason = REASONS.get(code, reason)
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
                    text += "\nСтатус в ответе сервера: " + human_status(
                        actual, "order" if kind == "fulfill" else "item"
                    )
                    if actual in {"PENDING_APPROVAL", "PENDING_MODERATION"}:
                        text += "\nОжидает модерации; активным пока не считается."
                if result.get("price") is not None:
                    text += "\nЦена: " + escaped(result["price"], 40) + " ₽"
                base = result.get("raw_price")
                if base is not None:
                    text += "\nБазовая цена: " + escaped(base, 40) + " ₽"
        if kind in {"premium_publish", "premium_bump"} and status == "SUCCEEDED":
            text += "\nПодтверждено списание: " + escaped(result.get("paid_rub"), 40) + " ₽"
            text += "\nПлатёж: <code>" + escaped(result.get("payment_id"), 100) + "</code>"
        if kind.startswith("gift_"):
            job = self.store.rows(
                "SELECT state,refund_state FROM gift_jobs WHERE order_id=?", (row["target"],)
            )
            if job:
                text += f"\nПодарок: {human_status(job[0]['state'])} · возврат: {human_status(job[0]['refund_state'])}"
                steps = self.store.rows(
                    "SELECT code,position,state,stars,error_code FROM gift_steps WHERE order_id=? ORDER BY position",
                    (row["target"],),
                )
                done = sum(g["state"] == "SUCCEEDED" for g in steps)
                text += f"\nПрогресс пакета: {done}/{len(steps)}"
                if done and done < len(steps):
                    text += "\nДоставлено частично. Остальные подарки не подтверждены; полный возврат и отметка выполнения запрещены."
                for g in steps:
                    text += f"\n{escaped(gift_name(g['code']), 70)} {g['position'] + 1}/{len(steps)} · {human_status(g['state'])} · {g['stars']} ⭐"
                    if g["error_code"]:
                        text += " · " + escaped(g["error_code"], 70)
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
                    [("🚦 Настроить и запустить до публикации", "launch:from:" + row["target"])],
                    [("📦 Лот и доступные действия", "item:" + target)],
                    [("📚 Склад / перенос из мастера", "stock:view:" + target)],
                ]
        elif kind in {"reply", "mark_read"}:
            buttons.append([("Диалог", "chat:" + row["target"])])
        elif kind != "delete":
            buttons.append([("Открыть актуальную карточку", "item:" + row["target"])])
        payload = json.loads(row["payload"])
        if (
            status == "SUCCEEDED"
            and (
                kind == "edit_fields"
                or kind == "edit"
                and "description" in payload
                and not payload.get("logical_binding")
                and not payload.get("code_migration")
            )
            and self.store.rows("SELECT 1 FROM logical_listings WHERE item_id=?", (row["target"],))
        ):
            text += (
                "\nИзменение сохранено. Выдача остаётся остановленной до повторного подтверждения привязки."
            )
            buttons.insert(0, [("🔐 Подтвердить привязку", "lp:item:" + row["target"])])
        buttons += [
            [("Подробности операции", "ops:show:" + ident)],
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
            [
                (
                    TITLES.get(row["kind"], row["kind"]) + " · " + human_status(row["status"]),
                    "result:show:" + row["id"],
                )
            ]
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

    async def refresh_views(self, bot):
        if getattr(bot, "id", None) is None:
            return
        rows = self.store.rows(
            "SELECT r.*,o.status FROM operation_receipts r JOIN outbox o ON o.id=r.operation_id WHERE r.view_message_id IS NOT NULL AND (r.rendered_status IS NULL OR r.rendered_status!=o.status) LIMIT 50"
        )
        for row in rows:
            lock = getattr(self.store, "navigation_locks", {}).get((bot.id, row["owner_id"]))
            if lock is None:
                lock = asyncio.Lock()
            async with lock:
                from app.telegram.navigation_context import panel_key

                key = panel_key(self.store, bot.id, row["owner_id"], row["view_message_id"])
                panel = self.store.setting(key, {})
                if (
                    panel.get("id") != row["view_message_id"]
                    or panel.get("token") != row["view_token"]
                    or panel.get("sending")
                    or panel.get("editing")
                ):
                    continue
                retry_key = f"receipt-ui:{row['operation_id']}:{row['owner_id']}"
                retry = self.store.setting(retry_key, {})
                if (
                    retry.get("view_token") != row["view_token"]
                    or retry.get("view_message_id") != row["view_message_id"]
                ):
                    retry = {"view_token": row["view_token"], "view_message_id": row["view_message_id"]}
                if retry.get("terminal") or retry.get("after", 0) > time.time():
                    continue
                text, markup = self.show(row["owner_id"], row["operation_id"])
                try:
                    await bot.edit_message_text(
                        text, chat_id=row["owner_id"], message_id=row["view_message_id"], reply_markup=markup
                    )
                except asyncio.CancelledError:
                    self.defer_view(retry_key, retry, TimeoutError())
                    raise
                except TelegramBadRequest as error:
                    if telegram_error(error) != "MESSAGE_NOT_MODIFIED":
                        self.defer_view(retry_key, retry, error)
                        continue
                except Exception as error:  # noqa: BLE001 -- retry only known edit, never a business action/send
                    self.defer_view(retry_key, retry, error)
                    continue
                self.store.set_setting(retry_key, {})
                self.store.execute(
                    "UPDATE operation_receipts SET rendered_status=?,notice_status=CASE WHEN ? IN ('SUCCEEDED','UNCERTAIN','PERMANENT_FAILED') THEN 'SENT' ELSE notice_status END WHERE operation_id=? AND owner_id=? AND view_token=?",
                    (row["status"], row["status"], row["operation_id"], row["owner_id"], row["view_token"]),
                )

    def defer_view(self, key, previous, error):
        failures = previous.get("failures", 0) + 1
        self.store.set_setting(
            key,
            {
                **previous,
                "code": telegram_error(error),
                "terminal": telegram_error(error) in TERMINAL_EDIT or failures >= 8,
                "failures": failures,
                "after": time.time()
                + max(min(3600, 5 * 2 ** min(failures, 10)), getattr(error, "retry_after", 0)),
            },
        )

    async def notify(self, bot):
        rows = self.store.rows(
            "WITH eligible AS (SELECT r.operation_id,r.owner_id,r.view_message_id,r.view_token,o.created_at,"
            "CASE o.status WHEN 'UNCERTAIN' THEN 1 ELSE 2 END priority FROM operation_receipts r "
            "JOIN outbox o ON o.id=r.operation_id WHERE r.notice_status='PENDING' AND r.send_retry_at<=? "
            "AND NOT (o.status='SUCCEEDED' AND o.operation_key LIKE 'chat-card-reply:%') "
            "AND NOT EXISTS(SELECT 1 FROM notification_actions a WHERE a.operation_id=o.id) "
            "AND o.status IN ('SUCCEEDED','UNCERTAIN','PERMANENT_FAILED')), ranked AS "
            "(SELECT *,row_number() OVER(PARTITION BY priority ORDER BY created_at,operation_id,owner_id) position FROM eligible) "
            "SELECT * FROM ranked WHERE position<=CASE priority WHEN 1 THEN 15 ELSE 5 END ORDER BY priority,created_at",
            (time.time(),),
        )
        for row in rows:
            from app.telegram.navigation_context import panel_key

            panel = self.store.setting(
                panel_key(self.store, getattr(bot, "id", None), row["owner_id"], row["view_message_id"]), {}
            )
            if (
                row["view_message_id"] is not None
                and panel.get("id") == row["view_message_id"]
                and panel.get("token") == row["view_token"]
            ):
                continue
            # Persist admission before Telegram; a lost Telegram ACK must not resend.
            changed = self.store.execute(
                "UPDATE operation_receipts SET notice_status='PROCESSING' WHERE operation_id=? AND owner_id=? AND notice_status='PENDING' AND send_retry_at<=?",
                (row["operation_id"], row["owner_id"], time.time()),
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
            except Exception as error:  # noqa: BLE001 -- no raw Telegram errors or buyer data
                outcome, code, delay = send_failure(error)
                self.store.execute(
                    "UPDATE operation_receipts SET notice_status=?,send_error=?,send_retry_at=? WHERE operation_id=? AND owner_id=?",
                    (outcome, code, time.time() + delay, row["operation_id"], row["owner_id"]),
                )
            else:
                self.store.execute(
                    "UPDATE operation_receipts SET notice_status='SENT',send_error=NULL,send_retry_at=0,telegram_message_id=? WHERE operation_id=? AND owner_id=?",
                    (message.message_id, row["operation_id"], row["owner_id"]),
                )

        await self.refresh_views(bot)
