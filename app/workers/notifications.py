import asyncio
import hashlib
import json
import time
from datetime import datetime
from enum import Enum
from html import escape

from aiogram.exceptions import TelegramBadRequest

from app.database.store import encode
from app.gifts.outcomes import DELAYED_PREFLIGHT_SQL
from app.services.chat_events import system_event
from app.services.notification_preferences import NotificationPreferences, notification_baseline
from app.telegram.chat_cards import ChatCards
from app.telegram.errors import TERMINAL_EDIT, send_failure, telegram_error
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.message_notice import message_notice
from app.telegram.operation_results import OperationResults
from app.telegram.presentation import status as human_status


class NoticeEdit(Enum):
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"


class Notifications:
    """One attempt per owner/event; uncertain Telegram sends require manual reconciliation."""

    def __init__(self, store, bot):
        self.store, self.bot = store, bot
        self.preferences = NotificationPreferences(store)
        notification_baseline(store)

    async def once(self):
        if not self.store.rows("SELECT user_id FROM administrators LIMIT 1"):
            return
        started = time.perf_counter()
        # Startup, not the last inaccessible historical chat, defines the boundary.
        boundary = notification_baseline(self.store)
        critical = (
            "(i.kind IN ('worker_failed','operation_uncertain','stock_empty','stock_low','order_problem','delivery_delayed',"
            "'promotion_guard','promotion_error','enrollment_blocked','logical_product_blocked') OR "
            "(i.kind='message' AND json_extract(i.payload,'$.user') IS NULL "
            "AND json_extract(i.payload,'$.text')='{{DEAL_HAS_PROBLEM}}'))"
        )
        transition = (
            "(i.kind='order' AND EXISTS(SELECT 1 FROM inbox prior WHERE prior.kind='order' "
            "AND prior.rowid<i.rowid AND json_extract(prior.payload,'$.id')=json_extract(i.payload,'$.id') "
            "AND json_extract(prior.payload,'$.status')<>json_extract(i.payload,'$.status')))"
        )
        fresh_event = (
            "(server_time(json_extract(i.payload,'$.created_at'))>server_time(?) OR " + transition + ")"
        )
        if boundary:
            self.store.execute(
                "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) "
                "SELECT i.key,a.user_id,'BASELINE' FROM inbox i CROSS JOIN administrators a WHERE NOT "
                + critical
                + " AND (i.rowid<=? OR (server_time(json_extract(i.payload,'$.created_at'))<=server_time(?) AND NOT "
                + transition
                + ")) AND NOT EXISTS(SELECT 1 FROM notifications n WHERE n.event_key=i.key AND n.owner_id=a.user_id)",
                (boundary["sequence"], boundary["at"]),
            )
        initialized = self.store.setting("notifications:initialized", False)
        if not initialized and self.store.setting("sync:health", {}).get("status") == "OK":
            with self.store.transaction() as db:
                db.execute(
                    "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("notifications:initialized", encode(True)),
                )
            initialized = True
        priority = (
            "CASE WHEN i.kind IN ('worker_failed','operation_uncertain','order_problem','delivery_delayed',"
            "'promotion_guard','enrollment_blocked','logical_product_blocked') "
            "OR (i.kind='order' AND json_extract(i.payload,'$.status')='PAID') "
            "OR (i.kind='message' AND json_extract(i.payload,'$.text')='{{DEAL_HAS_PROBLEM}}') THEN 1 "
            "WHEN i.kind IN ('message','order','stock_empty','stock_low','promotion_error') THEN 2 ELSE 3 END"
        )
        events = self.store.rows(
            "WITH eligible AS (SELECT i.*, "
            + priority
            + " priority FROM inbox i WHERE i.processed_at IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM administrators a WHERE NOT EXISTS "
            "(SELECT 1 FROM notifications n WHERE n.event_key=i.key AND n.owner_id=a.user_id) "
            "OR EXISTS(SELECT 1 FROM notifications n WHERE n.event_key=i.key AND n.owner_id=a.user_id "
            "AND n.status='PENDING' AND n.send_retry_at<=strftime('%s','now'))) "
            + ("AND (" + critical + " OR " + fresh_event + ") " if not initialized else "")
            + "), ranked AS (SELECT *,row_number() OVER(PARTITION BY priority ORDER BY received_at,key) position FROM eligible) "
            ", quota AS (SELECT * FROM ranked WHERE position<=CASE priority WHEN 1 THEN 100 WHEN 2 THEN 75 ELSE 25 END), "
            "extra AS (SELECT * FROM ranked WHERE position>CASE priority WHEN 1 THEN 100 WHEN 2 THEN 75 ELSE 25 END "
            "ORDER BY priority,received_at,key LIMIT (SELECT max(0,200-count(*)) FROM quota)) "
            "SELECT * FROM (SELECT * FROM quota UNION ALL SELECT * FROM extra) ORDER BY priority,received_at,key",
            (boundary["at"],) if not initialized else (),
        )
        owners = self.store.rows("SELECT user_id FROM administrators")
        for event in events:
            payload = json.loads(event["payload"])
            if event["kind"] == "message":
                author = payload.get("user") or {}
                account_id = self.store.setting("playerok:account_id")
                if (
                    payload.get("is_read") is True
                    or payload.get("deleted_at")
                    or (account_id and author.get("id") == account_id)
                ):
                    self.store.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) "
                        "SELECT ?,user_id,'SKIPPED' FROM administrators",
                        (event["key"],),
                    )
                    continue
                rows = self.store.rows("SELECT chat_id FROM chat_messages WHERE id=?", (payload["id"],))
                chat_id = rows[0]["chat_id"] if rows else None
                event_label = system_event(payload)
                deal_id = (payload.get("deal") or {}).get("id")
                if (
                    event_label
                    and deal_id
                    and self.store.rows(
                        "SELECT 1 FROM inbox WHERE kind='order' AND json_extract(payload,'$.id')=?",
                        (deal_id,),
                    )
                    and payload.get("text") != "{{DEAL_HAS_PROBLEM}}"
                ):
                    self.store.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) SELECT ?,user_id,'SKIPPED' FROM administrators",
                        (event["key"],),
                    )
                    continue
                text, markup = message_notice(payload, chat_id)
                if not event_label and chat_id:
                    for owner in owners:
                        user = owner["user_id"]
                        invalid = False
                        try:
                            enabled = self.preferences.enabled(user, "message")
                        except ValueError:
                            enabled, invalid = False, True
                            self.store.set_setting(
                                f"notifications:{user}:preferences_error", "INVALID_PREFERENCES"
                            )
                        with self.store.transaction() as db:
                            inserted = db.execute(
                                "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) VALUES(?,?,?)",
                                (
                                    event["key"],
                                    user,
                                    "CARD" if enabled else "SUPPRESSED_INVALID" if invalid else "SUPPRESSED",
                                ),
                            ).rowcount
                            if inserted and enabled:
                                ChatCards(self.store, self.bot).touch(user, chat_id)
                    continue
            elif event["kind"] == "order":
                current = self.store.rows("SELECT status FROM orders WHERE id=?", (payload["id"],))
                if not current or current[0]["status"] != payload["status"]:
                    self.store.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) SELECT ?,user_id,'SKIPPED' FROM administrators",
                        (event["key"],),
                    )
                    continue
                item = payload.get("item") or {}
                text = f"🛒 <b>Заказ</b>\n{escape(item.get('name') or payload['id'])}\nСтатус: {escape(human_status(payload['status'], 'order'))}\nСумма: {item.get('price', '?')} ₽"
                markup = keyboard([[("Открыть заказ", "order:" + payload["id"])]])
            elif event["kind"] == "worker_failed":
                from app.workers.supervisor import CYCLE_LABELS

                if payload.get("cycle"):
                    current = self.store.setting("runtime:cycle:" + payload["worker"], {})
                    if current.get("consecutive_failures", 0) < 3 or current.get("incident") != payload.get(
                        "incident"
                    ):
                        self.store.execute(
                            "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) "
                            "SELECT ?,user_id,'RESOLVED' FROM administrators",
                            (event["key"],),
                        )
                        continue
                text = (
                    (
                        "⚠️ <b>Фоновая обработка требует проверки</b>\n"
                        if payload.get("cycle")
                        else "⚠️ <b>Фоновая задача остановилась</b>\n"
                    )
                    + escaped(
                        CYCLE_LABELS.get(payload["worker"], payload["worker"])
                        if payload.get("cycle")
                        else payload["worker"],
                        80,
                    )
                    + ": "
                    + escaped(payload["error_type"], 80)
                    + (
                        "\nПовторная проверка запланирована. Проверьте диагностику. "
                        if payload.get("cycle")
                        else "\nПерезапуск выполняется с задержкой. Проверьте диагностику. "
                    )
                    + "Неопределённые операции автоматически не повторяются."
                )
                markup = keyboard(
                    [[("Диагностика", "diagnostics:workers" if payload.get("cycle") else "diagnostics")]]
                )
            elif event["kind"] == "promotion_error":
                health = self.store.setting("promotions:health", {})
                if health.get("status") != "DEGRADED" or health.get("incident") != payload.get("incident"):
                    self.store.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) SELECT ?,user_id,'RESOLVED' FROM administrators",
                        (event["key"],),
                    )
                    continue
                text = (
                    "⚠️ <b>Проверки автопродвижения повторно завершаются ошибкой</b>\n"
                    f"Товаров с ошибками: {health.get('failed_items', 0)}. "
                    "Исправные товары проверяются независимо. Повтор запланирован; "
                    "это предупреждение не подтверждает покупку поднятия."
                )
                markup = keyboard([[("Проверить автопродвижение", "promotions")]])
            elif event["kind"] == "promotion_guard":
                text = (
                    "⛔ <b>Политика продвижения выключена по лимиту</b>\n"
                    + escaped("; ".join(payload["reasons"]), 800)
                    + "\nПроверка была dry-run; списаний не было."
                )
                if payload.get("paid_schedule_block"):
                    text = (
                        "⛔ <b>Платное поднятие остановлено</b>\n"
                        + escaped("; ".join(payload["reasons"]), 800)
                        + "\nНовое действие не поставлено в очередь. Проверьте тариф и разрешения."
                    )
                markup = keyboard([[("Настройки продвижения", "promotions")]])
            elif event["kind"] == "enrollment_blocked":
                text = (
                    "⚠️ <b>Автоматическая выдача не запущена</b>\nЗаказ: "
                    + escaped(payload["order_id"], 100)
                    + "\nТребуется проверка владельца. Сбой условий не запускает слепой повтор."
                )
                markup = keyboard([[("Заказ", "order:" + payload["order_id"])]])
            elif event["kind"] == "logical_product_blocked":
                rows = self.store.rows(
                    "SELECT p.code,p.error_code,l.item_id FROM logical_products p JOIN logical_listings l ON l.product_id=p.id WHERE p.id=? LIMIT 1",
                    (payload["product_id"],),
                )
                if not rows or rows[0]["error_code"] != payload["code"]:
                    self.store.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) SELECT ?,user_id,'SKIPPED' FROM administrators",
                        (event["key"],),
                    )
                    continue
                from app.services.logical_products import PROBLEMS

                text = (
                    "⚠️ <b>Проверьте постоянный код товара</b>\n"
                    + escaped(rows[0]["code"], 20)
                    + "\n"
                    + escaped(
                        PROBLEMS.get(payload["code"], "Связь объявления с настройкой выдачи не подтверждена"),
                        300,
                    )
                )
                markup = keyboard([[("Проверить товар", "lp:item:" + rows[0]["item_id"])]])
            elif event["kind"] in {"stock_empty", "stock_low"}:
                from app.services.inventory import Inventory

                inventory = Inventory(self.store, None)
                available = inventory.counts(payload["item_id"])["AVAILABLE"]
                pause_operation = payload.get("pause_operation")
                pause_finished = pause_operation and self.store.rows(
                    "SELECT 1 FROM outbox WHERE id=? AND status='SUCCEEDED'", (pause_operation,)
                )
                generation = self.store.rows(
                    "SELECT MAX(id) AS id FROM inventory WHERE item_id=?",
                    (inventory.scope(payload["item_id"]),),
                )[0]["id"]
                if (
                    pause_finished
                    or available != (1 if event["kind"] == "stock_low" else 0)
                    or (event["kind"] == "stock_low" and payload.get("generation") != generation)
                ):
                    self.store.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) SELECT ?,user_id,'SKIPPED' FROM administrators",
                        (event["key"],),
                    )
                    continue
                if pause_operation:
                    text = (
                        "⚠️ <b>Playerok не разрешил снять товар с продажи</b>\n"
                        "Склад пуст. Объявление может оставаться доступным для покупки. "
                        "Бот не выдаст отсутствующий товар и повторит безопасную проверку."
                    )
                else:
                    text = (
                        "📦 <b>На складе осталась 1 единица</b>\nПополните запас, чтобы продажи не остановились."
                        if event["kind"] == "stock_low"
                        else "📦 <b>Доступный запас закончился</b>\nНовые выдачи остановлены до пополнения. Уже зарезервированные товары будут выданы; карантинные данные не используются повторно."
                    )
                markup = keyboard([[("Пополнить склад", "stock:add:" + payload["item_id"])]])
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
            elif event["kind"] == "delivery_delayed":
                incident = self.store.setting("gifts:preflight_incident", {})
                waiting = self.store.rows(DELAYED_PREFLIGHT_SQL)
                if not waiting or incident.get("id") != payload.get("incident"):
                    if not waiting:
                        self.store.execute(
                            "UPDATE settings SET value='{}' WHERE key='gifts:preflight_incident' AND json_extract(value,'$.id')=?",
                            (payload.get("incident"),),
                        )
                    self.store.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) SELECT ?,user_id,'SKIPPED' FROM administrators",
                        (event["key"],),
                    )
                    continue
                text = (
                    "⚠️ <b>Выдача ожидает восстановления</b>\n"
                    + f"Заказов с задержкой более пяти минут: {len({row['target'] for row in waiting})}.\n"
                    + "Ожидающие действия ещё не отправлялись. Бот продолжает безопасные проверки. "
                    + "Если подключение не восстанавливается, проверьте прокси и соединение. Повторно включать товары не нужно."
                )
                markup = keyboard([[("Оплаченные заказы", "orderpage:0:PAID")]])
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
                    invalid = False
                    try:
                        enabled = self.preferences.enabled(user, event["kind"])
                    except ValueError:
                        enabled, invalid = False, True
                        db.execute(
                            "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (f"notifications:{user}:preferences_error", encode("INVALID_PREFERENCES")),
                        )
                    if not enabled:
                        db.execute(
                            "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) VALUES(?,?,?)",
                            (event["key"], user, "SUPPRESSED_INVALID" if invalid else "SUPPRESSED"),
                        )
                        continue
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) VALUES(?,?,'PROCESSING')",
                        (event["key"], user),
                    ).rowcount
                if not inserted:
                    inserted = self.store.execute(
                        "UPDATE notifications SET status='PROCESSING' WHERE event_key=? AND owner_id=? AND status='PENDING' AND send_retry_at<=?",
                        (event["key"], user, time.time()),
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
                    outcome, code, delay = send_failure(error)
                    self.store.execute(
                        "UPDATE notifications SET status=?,send_error=?,send_retry_at=? WHERE event_key=? AND owner_id=?",
                        (outcome, code, time.time() + delay, event["key"], user),
                    )
                    self.store.audit(user, "NOTIFICATION", event["key"], type(error).__name__)
                else:
                    self.store.execute(
                        "UPDATE notifications SET status='SUCCEEDED',send_error=NULL,send_retry_at=0,telegram_message_id=?,rendered_payload=? WHERE event_key=? AND owner_id=?",
                        (
                            message.message_id,
                            hashlib.sha256(event["payload"].encode()).hexdigest(),
                            event["key"],
                            user,
                        ),
                    )

        dispatch_ms = (time.perf_counter() - started) * 1000
        background_started = time.perf_counter()
        exhausted = []
        # New events have already received their fair priority quota. Old edits
        # cannot monopolize the next cycle. Receipt cancellation retains the
        # existing PROCESSING -> UNCERTAIN contract; no business action repeats.
        for name, callback in (
            ("chats", lambda: ChatCards(self.store, self.bot).flush()),
            ("receipts", lambda: OperationResults(self.store).notify(self.bot)),
            ("edits", self.refresh_actions),
        ):
            try:
                async with asyncio.timeout(2):
                    await callback()
            except TimeoutError:
                exhausted.append(name)
        critical_ages = [
            max(0, time.time() - datetime.fromisoformat(event["received_at"]).timestamp())
            for event in events
            if event["priority"] == 1
        ]
        self.store.set_setting(
            "notifications:timing",
            {
                "at": time.time(),
                "dispatch_ms": round(dispatch_ms, 2),
                "background_ms": round((time.perf_counter() - background_started) * 1000, 2),
                "background_budget_exhausted": exhausted,
                "critical_max_age_seconds": round(max(critical_ages, default=0), 2),
                "events": len(events),
            },
        )

    async def refresh_messages(self):
        rows = self.store.rows(
            "SELECT n.*,i.payload,m.chat_id FROM notifications n JOIN inbox i ON i.key=n.event_key JOIN chat_messages m ON n.event_key='message:'||m.id WHERE n.status='SUCCEEDED' AND n.telegram_message_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM chat_cards c WHERE c.chat_id=m.chat_id AND c.owner_id=n.owner_id) AND NOT EXISTS(SELECT 1 FROM notification_actions a WHERE a.event_key=n.event_key AND a.owner_id=n.owner_id) AND n.refresh_failures>=0 AND n.refresh_after<=? LIMIT 100",
            (time.time(),),
        )
        for row in rows:
            fingerprint = hashlib.sha256(row["payload"].encode()).hexdigest()
            if fingerprint == row["rendered_payload"]:
                continue
            payload = json.loads(row["payload"])
            text, markup = message_notice(payload, row["chat_id"])
            if payload.get("deleted_at") or payload.get("is_read") is True:
                markup = keyboard([[("Диалог", "chat:" + row["chat_id"])]])
            try:
                outcome = await self.edit_notice(
                    text, chat_id=row["owner_id"], message_id=row["telegram_message_id"], reply_markup=markup
                )
                if outcome is NoticeEdit.SKIPPED:
                    self.store.execute(
                        "UPDATE notifications SET refresh_failures=-1 WHERE event_key=? AND owner_id=?",
                        (row["event_key"], row["owner_id"]),
                    )
                    continue
            except asyncio.CancelledError:
                self.store.execute(
                    "UPDATE notifications SET refresh_after=? WHERE event_key=? AND owner_id=?",
                    (time.time() + 30, row["event_key"], row["owner_id"]),
                )
                raise
            except TelegramBadRequest as error:
                if telegram_error(error) != "MESSAGE_NOT_MODIFIED":
                    terminal = telegram_error(error) in TERMINAL_EDIT or row["refresh_failures"] + 1 >= 8
                    self.store.execute(
                        "UPDATE notifications SET refresh_after=?,refresh_failures=? WHERE event_key=? AND owner_id=?",
                        (
                            time.time() + 300,
                            -1 if terminal else row["refresh_failures"] + 1,
                            row["event_key"],
                            row["owner_id"],
                        ),
                    )
                    self.store.audit(
                        row["owner_id"], "NOTIFICATION_UPDATE", row["event_key"], telegram_error(error)
                    )
                    continue
            except Exception as error:  # noqa: BLE001 -- only idempotent edits; no new message
                self.store.execute(
                    "UPDATE notifications SET refresh_after=?,refresh_failures=? WHERE event_key=? AND owner_id=?",
                    (
                        time.time()
                        + max(
                            min(3600, 5 * 2 ** min(row["refresh_failures"] + 1, 10)),
                            getattr(error, "retry_after", 0),
                        ),
                        -1 if row["refresh_failures"] + 1 >= 8 else row["refresh_failures"] + 1,
                        row["event_key"],
                        row["owner_id"],
                    ),
                )
                continue
            self.store.execute(
                "UPDATE notifications SET rendered_payload=?,refresh_after=0,refresh_failures=0 WHERE event_key=? AND owner_id=?",
                (fingerprint, row["event_key"], row["owner_id"]),
            )

    async def refresh_actions(self):
        await self.refresh_messages()
        pending = self.store.rows(
            "SELECT DISTINCT a.event_key,a.owner_id,n.telegram_message_id,n.rendered_payload,i.payload,m.chat_id "
            "FROM notification_actions a JOIN outbox o ON o.id=a.operation_id "
            "JOIN notifications n ON n.event_key=a.event_key AND n.owner_id=a.owner_id "
            "JOIN inbox i ON i.key=a.event_key JOIN chat_messages m ON i.key='message:'||m.id "
            "WHERE n.telegram_message_id IS NOT NULL "
            "AND a.failures>=0 AND a.retry_after<=? AND (a.rendered_status IS NULL OR a.rendered_status!=o.status OR n.rendered_payload IS NULL) LIMIT 100",
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
                "JOIN outbox o ON o.id=a.operation_id WHERE a.owner_id=? AND a.event_key=? AND a.failures>=0 AND a.retry_after<=? AND (a.rendered_status IS NULL OR a.rendered_status!=o.status OR ?=1) "
                "ORDER BY o.created_at DESC,o.id DESC",
                (
                    notification["owner_id"],
                    notification["event_key"],
                    time.time(),
                    int(notification["rendered_payload"] is None),
                ),
            )
            if not actions:
                continue
            # Render from the whole event history, not only the dirty/backoff subset.
            # A late older reply must not undo a newer confirmed read action.
            history = self.store.rows(
                "SELECT o.kind,o.status,o.result,o.id AS operation_id FROM notification_actions a "
                "JOIN outbox o ON o.id=a.operation_id WHERE a.owner_id=? AND a.event_key=? "
                "ORDER BY o.created_at DESC,o.id DESC",
                (notification["owner_id"], notification["event_key"]),
            )
            latest = history[0]
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
            if any(a["kind"] == "mark_read" and a["status"] == "SUCCEEDED" for a in history):
                for row in markup.inline_keyboard:
                    row[:] = [b for b in row if not (b.callback_data or "").startswith("readchat:")]
                markup.inline_keyboard = [row for row in markup.inline_keyboard if row]
            try:
                outcome = await self.edit_notice(
                    text,
                    chat_id=notification["owner_id"],
                    message_id=notification["telegram_message_id"],
                    reply_markup=markup,
                )
                if outcome is NoticeEdit.SKIPPED:
                    self.store.execute(
                        "UPDATE notification_actions SET failures=-1 WHERE event_key=? AND owner_id=?",
                        (notification["event_key"], notification["owner_id"]),
                    )
                    continue
            except asyncio.CancelledError:
                self.defer_update(notification, actions, TimeoutError())
                raise
            except TelegramBadRequest as error:
                if telegram_error(error) != "MESSAGE_NOT_MODIFIED":
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
                db.execute(
                    "UPDATE notifications SET rendered_payload=? WHERE event_key=? AND owner_id=?",
                    (
                        hashlib.sha256(notification["payload"].encode()).hexdigest(),
                        notification["event_key"],
                        notification["owner_id"],
                    ),
                )

                for action in actions:
                    db.execute(
                        "UPDATE notification_actions SET rendered_status=?,retry_after=0,failures=0 WHERE operation_id=?",
                        (action["status"], action["operation_id"]),
                    )

    async def edit_notice(self, text, *, chat_id, message_id, **kwargs):
        from app.telegram.navigation_context import panel_key

        bot_id = getattr(self.bot, "id", None)
        if not hasattr(self.store, "navigation_locks"):
            self.store.navigation_locks = {}
        key = panel_key(self.store, bot_id, chat_id, message_id)
        lock = self.store.navigation_locks.setdefault((bot_id, chat_id, key), asyncio.Lock())
        async with lock:
            key = panel_key(self.store, bot_id, chat_id, message_id)
            panel = self.store.setting(key, {})
            if panel.get("closed") or panel.get("route") is not None:
                return NoticeEdit.SKIPPED
            try:
                await self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, **kwargs)
            except TelegramBadRequest as error:
                if telegram_error(error) != "MESSAGE_NOT_MODIFIED":
                    raise
                return NoticeEdit.UNCHANGED
            return NoticeEdit.UPDATED

    def defer_update(self, notification, actions, error):
        for action in actions:
            failures = (
                -1
                if telegram_error(error) in TERMINAL_EDIT or action["failures"] + 1 >= 8
                else action["failures"] + 1
            )
            delay = max(min(3600, 5 * 2 ** min(failures, 10)), getattr(error, "retry_after", 0))
            self.store.execute(
                "UPDATE notification_actions SET retry_after=?,failures=? WHERE operation_id=? AND event_key=? AND owner_id=?",
                (
                    time.time() + delay,
                    failures,
                    action["operation_id"],
                    notification["event_key"],
                    notification["owner_id"],
                ),
            )

    async def run(self, stop):
        from app.workers.supervisor import worker_cycle

        self.store.execute("UPDATE notifications SET status='UNCERTAIN' WHERE status='PROCESSING'")
        self.store.execute(
            "UPDATE operation_receipts SET notice_status='UNCERTAIN' WHERE notice_status='PROCESSING'"
        )
        while not stop.is_set():
            with worker_cycle(self.store, "notifications", 5):
                await self.once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                continue
