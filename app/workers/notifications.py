import asyncio
import json
import time
from html import escape

from aiogram.exceptions import TelegramBadRequest

from app.database.store import encode
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.message_notice import message_notice


class Notifications:
    """One attempt per owner/event; uncertain Telegram sends require manual reconciliation."""

    def __init__(self, store, bot):
        self.store, self.bot = store, bot

    async def once(self):
        if not self.store.rows("SELECT user_id FROM administrators LIMIT 1"):
            return
        await self.refresh_actions()
        # First completed sync establishes baseline: do not flood with historical orders.
        initialized = self.store.setting("notifications:initialized", False)
        if not initialized and self.store.setting("sync:health", {}).get("status") == "OK":
            with self.store.transaction() as db:
                db.execute(
                    "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) SELECT i.key,a.user_id,'BASELINE' FROM inbox i CROSS JOIN administrators a WHERE i.kind NOT IN ('operation_uncertain','stock_empty','order_problem')"
                )
                db.execute(
                    "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("notifications:initialized", encode(True)),
                )
            initialized = True
        events = self.store.rows(
            "SELECT i.* FROM inbox i WHERE i.processed_at IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM administrators a WHERE NOT EXISTS "
            "(SELECT 1 FROM notifications n WHERE n.event_key=i.key AND n.owner_id=a.user_id)) "
            + (
                "AND i.kind IN ('operation_uncertain','stock_empty','order_problem') "
                if not initialized
                else ""
            )
            + "ORDER BY i.received_at,i.key LIMIT 200"
        )
        owners = self.store.rows("SELECT user_id FROM administrators")
        for event in events:
            payload = json.loads(event["payload"])
            if event["kind"] == "message":
                author = payload.get("user") or {}
                account_id = self.store.setting("playerok:account_id")
                if account_id and author.get("id") == account_id:
                    self.store.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) "
                        "SELECT ?,user_id,'SKIPPED' FROM administrators",
                        (event["key"],),
                    )
                    continue
                rows = self.store.rows("SELECT chat_id FROM chat_messages WHERE id=?", (payload["id"],))
                chat_id = rows[0]["chat_id"] if rows else None
                text, markup = message_notice(payload, chat_id)
            elif event["kind"] == "order":
                current = self.store.rows("SELECT status FROM orders WHERE id=?", (payload["id"],))
                if not current or current[0]["status"] != payload["status"]:
                    self.store.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) SELECT ?,user_id,'SKIPPED' FROM administrators",
                        (event["key"],),
                    )
                    continue
                item = payload.get("item") or {}
                text = f"🛒 <b>Заказ</b>\n{escape(item.get('name') or payload['id'])}\nСтатус: {escape(payload['status'])}\nСумма: {item.get('price', '?')} ₽"
                markup = keyboard([[("Открыть заказ", "order:" + payload["id"])]])
            elif event["kind"] == "stock_empty":
                if self.store.rows(
                    "SELECT 1 FROM inventory WHERE item_id=? AND status='AVAILABLE' LIMIT 1",
                    (payload["item_id"],),
                ):
                    self.store.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) SELECT ?,user_id,'SKIPPED' FROM administrators",
                        (event["key"],),
                    )
                    continue
                text = "📦 <b>Доступный запас закончился</b>\nАвтовыдача этого товара выключена. Пустые данные не отправляются; зарезервированные и карантинные коды не используются повторно."
                markup = keyboard([[("Открыть заказ", "order:" + payload["order_id"])]])
            elif event["kind"] == "order_problem":
                if not self.store.rows(
                    "SELECT 1 FROM orders WHERE id=? AND json_extract(payload,'$.has_problem')=1",
                    (payload["order_id"],),
                ):
                    self.store.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) SELECT ?,user_id,'SKIPPED' FROM administrators",
                        (event["key"],),
                    )
                    continue
                text = "❗ <b>Проблема в сделке Playerok</b>\n" + escaped(payload["item_name"], 300)
                markup = keyboard([[("Открыть заказ", "order:" + payload["order_id"])]])
            elif event["kind"] == "operation_uncertain":
                ident = payload["operation_id"]
                current = self.store.rows("SELECT status FROM outbox WHERE id=?", (ident,))
                if not current or current[0]["status"] != "UNCERTAIN":
                    self.store.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) SELECT ?,user_id,'SKIPPED' FROM administrators",
                        (event["key"],),
                    )
                    continue
                text = (
                    "⚠️ <b>Неопределённый результат операции</b>\n"
                    + escaped(payload["kind"], 80)
                    + "\nID: <code>"
                    + escaped(ident, 100)
                    + "</code>\n"
                    + "Автоматического повтора нет. Откройте операцию и проверьте результат на Playerok."
                )
                markup = keyboard([[("🔎 Проверить операцию", "ops:show:" + ident)]])
            else:
                self.store.execute(
                    "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) "
                    "SELECT ?,user_id,'SKIPPED' FROM administrators",
                    (event["key"],),
                )
                continue
            for owner in owners:
                user = owner["user_id"]
                with self.store.transaction() as db:
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) VALUES(?,?,'PROCESSING')",
                        (event["key"], user),
                    ).rowcount
                if not inserted:
                    continue
                try:
                    message = await self.bot.send_message(user, text, reply_markup=markup)
                except asyncio.CancelledError:
                    self.store.execute(
                        "UPDATE notifications SET status='UNCERTAIN' WHERE event_key=? AND owner_id=?",
                        (event["key"], user),
                    )
                    raise
                except Exception as error:  # noqa: BLE001 -- API may have accepted message; never retry blindly
                    self.store.execute(
                        "UPDATE notifications SET status='UNCERTAIN' WHERE event_key=? AND owner_id=?",
                        (event["key"], user),
                    )
                    self.store.audit(user, "NOTIFICATION", event["key"], type(error).__name__)
                else:
                    self.store.execute(
                        "UPDATE notifications SET status='SUCCEEDED',telegram_message_id=? WHERE event_key=? AND owner_id=?",
                        (message.message_id, event["key"], user),
                    )

    async def refresh_actions(self):
        pending = self.store.rows(
            "SELECT DISTINCT a.event_key,a.owner_id,n.telegram_message_id,i.payload,m.chat_id "
            "FROM notification_actions a JOIN outbox o ON o.id=a.operation_id "
            "JOIN notifications n ON n.event_key=a.event_key AND n.owner_id=a.owner_id "
            "JOIN inbox i ON i.key=a.event_key JOIN chat_messages m ON i.key='message:'||m.id "
            "WHERE n.telegram_message_id IS NOT NULL "
            "AND a.retry_after<=? AND (a.rendered_status IS NULL OR a.rendered_status!=o.status) LIMIT 100",
            (time.time(),),
        )
        labels = {
            "PENDING": "сохранено в очереди",
            "PROCESSING": "выполняется",
            "RETRYABLE_FAILED": "ожидает повторной проверки",
            "UNCERTAIN": "результат неизвестен; проверьте Playerok. Автоповтора нет",
            "PERMANENT_FAILED": "не выполнено; см. диагностику",
            "SUCCEEDED": "подтверждено API",
        }
        for notification in pending:
            actions = self.store.rows(
                "SELECT a.operation_id,a.failures,o.kind,o.status,o.result FROM notification_actions a "
                "JOIN outbox o ON o.id=a.operation_id WHERE a.owner_id=? AND a.event_key=? "
                "ORDER BY o.created_at DESC,o.id DESC",
                (notification["owner_id"], notification["event_key"]),
            )
            latest = actions[0]
            name = "Ответ" if latest["kind"] == "reply" else "Отметка прочитанности"
            note = (
                f"<b>{name}:</b> {labels[latest['status']]}\nОперация: <code>{latest['operation_id']}</code>"
            )
            result = json.loads(latest["result"]) if latest["result"] else {}
            if result and result.get("resolution_source") == "OWNER_DECISION":
                state_label = (
                    "повтор остановлен; внешний результат неизвестен"
                    if result.get("decision") == "STOP_UNCERTAIN"
                    else "подтверждено владельцем после сверки"
                )
                note = f"<b>{name}:</b> {state_label}\nОперация: <code>{latest['operation_id']}</code>"
            if latest["status"] == "SUCCEEDED" and result.get("message_id"):
                note += "\nID ответа: " + escaped(result["message_id"], 100)
            text, markup = message_notice(json.loads(notification["payload"]), notification["chat_id"], note)
            try:
                await self.bot.edit_message_text(
                    text,
                    chat_id=notification["owner_id"],
                    message_id=notification["telegram_message_id"],
                    reply_markup=markup,
                )
            except asyncio.CancelledError:
                raise
            except TelegramBadRequest as error:
                if "message is not modified" not in error.message.lower():
                    self.store.audit(
                        notification["owner_id"],
                        "NOTIFICATION_UPDATE",
                        notification["event_key"],
                        "TELEGRAM_REJECTED",
                    )
                    self.defer_update(notification, actions, error)
                    continue
            except Exception as error:  # noqa: BLE001 -- editing same message is safe to retry, never resend buyer reply
                self.store.audit(
                    notification["owner_id"],
                    "NOTIFICATION_UPDATE",
                    notification["event_key"],
                    type(error).__name__,
                )
                self.defer_update(notification, actions, error)
                continue
            with self.store.transaction() as db:
                for action in actions:
                    db.execute(
                        "UPDATE notification_actions SET rendered_status=?,retry_after=0,failures=0 WHERE operation_id=?",
                        (action["status"], action["operation_id"]),
                    )

    def defer_update(self, notification, actions, error):
        failures = max(action["failures"] for action in actions) + 1
        delay = max(min(3600, 5 * 2 ** min(failures, 10)), getattr(error, "retry_after", 0))
        self.store.execute(
            "UPDATE notification_actions SET retry_after=?,failures=? WHERE event_key=? AND owner_id=?",
            (time.time() + delay, failures, notification["event_key"], notification["owner_id"]),
        )

    async def run(self, stop):
        self.store.execute("UPDATE notifications SET status='UNCERTAIN' WHERE status='PROCESSING'")
        while not stop.is_set():
            await self.once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                continue
