"""Owner-facing durable receipts, using the existing notification loop."""

import asyncio
import json
import time
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton

from app.database.store import Conflict
from app.gifts.core import gift_name
from app.services.edit_confirmation import is_ordinary_edit
from app.telegram.errors import TERMINAL_EDIT, send_failure, telegram_error
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.presentation import edit_readback_note
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
    def __init__(self, store, api=None):
        self.store, self.api = store, api

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
        closed_edit = is_ordinary_edit(row) and row["error_code"] == "MANUALLY_STOPPED_UNCERTAIN"
        if closed_edit:
            label = "Запись правки закрыта. Выдача этой кнопкой не выключалась."
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
        if (
            status == "SUCCEEDED"
            and kind == "edit"
            and {"add_photos", "keep_attachment_ids"} & payload.keys()
        ):
            text += "\nФотографии: состав подтверждён. API не подтверждает порядок и главное фото; проверьте их на Playerok."
        if status in {"PENDING", "PROCESSING", "RETRYABLE_FAILED"}:
            text += "\nПостановка в очередь не означает выполнения."
        elif status == "UNCERTAIN" or row["error_code"] == "MANUALLY_STOPPED_UNCERTAIN":
            text += "\n⚠️ Запрос мог выполниться. Не повторяйте его. Сначала проверьте фактический результат."
            if kind == "edit":
                text += edit_readback_note(self.store.setting("edit:confirmation:" + ident, {}))
                if closed_edit:
                    buttons.append([("🔍 Проверить товар и выдачу", "rd:check:" + row["target"])])
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
            if (
                code in {"HTTP_500", "HTTP_502", "HTTP_503", "HTTP_504", "HTTP_429", "NETWORK_ERROR"}
                and row["started_at"] is None
            ):
                reason = "Playerok не вернул данные предварительной проверки. Бот не отправлял запрос на изменение или оплату; сохранённые настройки не отменены."
            elif code.startswith("HTTP_"):
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
                    [("🚀 Подготовить запуск", "launch:from:" + row["target"])],
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
                and not payload.get("marketing_description")
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
            from app.telegram.navigation_context import panel_key

            key = panel_key(self.store, bot.id, row["owner_id"], row["view_message_id"])
            if not hasattr(self.store, "navigation_locks"):
                self.store.navigation_locks = {}
            lock = self.store.navigation_locks.setdefault((bot.id, row["owner_id"], key), asyncio.Lock())
            async with lock:
                key = panel_key(self.store, bot.id, row["owner_id"], row["view_message_id"])
                panel = self.store.setting(key, {})
                if (
                    panel.get("closed")
                    or panel.get("id") != row["view_message_id"]
                    or panel.get("token") != row["view_token"]
                ):
                    self.store.execute(
                        "UPDATE operation_receipts SET view_message_id=NULL,view_token=NULL WHERE operation_id=? AND owner_id=? AND view_message_id=? AND view_token=?",
                        (row["operation_id"], row["owner_id"], row["view_message_id"], row["view_token"]),
                    )
                    continue
                if panel.get("sending") or panel.get("editing"):
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

    def promotion_report(self, owner):
        from app.services.premium_review import release_ids

        released = release_ids(self.store)
        policy_rows = self.store.rows(
            "SELECT actor,revision,selection,payload FROM promotion_policy WHERE id=1"
        )
        operations = self.store.rows(
            "SELECT * FROM (SELECT o.*,r.notice_status,row_number() OVER(PARTITION BY o.target ORDER BY o.created_at DESC,o.id DESC) AS latest "
            "FROM outbox o JOIN operation_receipts r ON r.operation_id=o.id "
            "WHERE r.owner_id=? AND o.kind='premium_bump' AND json_extract(o.payload,'$.automatic')=1 "
            ") WHERE latest=1 ORDER BY created_at DESC,id DESC",
            (owner,),
        )
        policy = policy_rows[0] if policy_rows and policy_rows[0]["actor"] == owner else None
        selected = (
            self.store.rows(
                "SELECT s.item_id FROM promotion_schedule s LEFT JOIN promotion_targets t ON t.item_id=s.item_id "
                "WHERE t.disposition='include' OR (?='all' AND coalesce(t.disposition,'default')!='exclude')",
                (policy["selection"] if policy else "selected",),
            )
            if policy
            else []
        )
        latest = {}
        for row in operations:
            latest.setdefault(row["target"], row)
        targets = dict.fromkeys([row["item_id"] for row in selected])
        for row in operations:
            if (
                row["notice_status"] == "PENDING"
                or row["status"] == "UNCERTAIN"
                and row["id"] not in released
                or row["status"] == "PERMANENT_FAILED"
                and row["started_at"] is not None
            ):
                targets.setdefault(row["target"], None)
        if not targets:
            return None
        lines = ["🚀 <b>Автоподнятие</b>"]
        if policy and not json.loads(policy["payload"])["enabled"]:
            lines.append("Выключено")
        links = []
        times = []
        confirmed = []
        seen = False
        for target in targets:
            evaluations = self.store.rows(
                "SELECT result,checked_at FROM promotion_evaluations WHERE item_id=? AND policy_revision=? ORDER BY id DESC LIMIT 1",
                (target, policy["revision"] if policy else -1),
            )
            evaluation = json.loads(evaluations[0]["result"]) if evaluations else {}
            row = latest.get(target)
            if row:
                times.append(row["created_at"])
            if evaluations:
                times.append(evaluations[0]["checked_at"])
            result = json.loads(row["result"] or "null") or {} if row else {}
            name = evaluation.get("item_name") or result.get("name") or target
            position = evaluation.get("position")
            if (
                row
                and (
                    row["status"] == "SUCCEEDED"
                    or row["status"] == "PERMANENT_FAILED"
                    and row["started_at"] is None
                )
                and row["notice_status"] != "PENDING"
                and evaluations
                and datetime.fromisoformat(evaluations[0]["checked_at"])
                > datetime.fromisoformat(row["created_at"])
            ):
                row = None  # A later check reports current conditions, not an old purchase as a new one.
            if row:
                seen = True
                if row["status"] == "SUCCEEDED":
                    label = "✅ успешно · " + str(result.get("paid_rub", "?")) + " ₽"
                elif row["status"] == "UNCERTAIN":
                    intent = self.store.setting("promotions:intent:" + row["id"], {})
                    acknowledged = intent.get("mutation_returned") or "candidate_id" in intent
                    age = (
                        time.time()
                        - datetime.fromisoformat(row["started_at"] or row["created_at"]).timestamp()
                    )
                    label = (
                        "⏳ проверяем списание"
                        if acknowledged and age < 120
                        else "⚠️ расход не подтверждён; повтор заблокирован"
                    )
                    if row["id"] in released:
                        label = "расход учтён; исход неизвестен"
                    elif not acknowledged or age >= 120:
                        links.append([("Проверить платёж", "ops:show:" + row["id"])])
                elif row["status"] in {"PENDING", "PROCESSING", "RETRYABLE_FAILED"}:
                    label = "⏳ ожидает обработки"
                elif row["started_at"] is not None:
                    label = "повтор закрыт; исход платежа неизвестен"
                else:
                    label = "покупка не отправлена; условия изменились"
            elif evaluation:
                seen = True
                reasons = evaluation.get("reasons") or []
                prefix = f"позиция {position} · " if type(position) is int else ""
                label = prefix + (
                    "поднятие не требуется"
                    if any(reason.startswith("Позиция в категории:") for reason in reasons)
                    else "; ".join(reasons) or "ожидает покупки"
                )
            else:
                label = "ожидает проверки"
            schedule = self.store.rows(
                "SELECT error_code,failures,checked_at FROM promotion_schedule WHERE item_id=?", (target,)
            )
            if (
                schedule
                and schedule[0]["error_code"]
                and (
                    row is None
                    or row["status"] == "SUCCEEDED"
                    and row["notice_status"] != "PENDING"
                    and schedule[0]["checked_at"]
                    and schedule[0]["checked_at"] > row["created_at"]
                )
            ):
                seen = True
                label = (
                    "⚠️ проверка недоступна; бот повторит"
                    if schedule[0]["failures"] >= 3
                    else "проверка повторится автоматически"
                )
            line = escaped(name, 170) + " — " + escaped(label, 260)
            if len(("\n".join(lines) + "\n" + line).encode("utf-16-le")) > 7200:
                lines.append("Остальные товары — в автоподнятии магазина.")
                break
            lines.append(line)
            if (
                row
                and row["status"] == "SUCCEEDED"
                and result.get("payment_id")
                and result.get("paid_rub") is not None
            ):
                confirmed.append(row)
        if times:
            at = max(datetime.fromisoformat(value) for value in times)
            lines[0] += " · " + at.astimezone(ZoneInfo("Europe/Moscow")).strftime("%H:%M")
        return ("\n".join(lines), keyboard(links, home=False), operations, confirmed) if seen else None

    async def promotion_totals(self, owner, rows):
        from app.playerok.adapter import APIError
        from app.security.logging import event
        from app.services.finance import money

        if not rows:
            return ""
        by_receipt = {
            result["payment_id"]: (row, result) for row in rows for result in (json.loads(row["result"]),)
        }
        ids = sorted(row["id"] for row, _ in by_receipt.values())
        key = f"notifications:promotion-totals:{owner}"
        previous = self.store.setting(key, {})
        if previous.get("ids") == ids:
            return previous["text"]
        try:
            spent = sum(money(result["paid_rub"]) for _, result in by_receipt.values())
        except ValueError:
            return ""
        text = f"Поднято лотов: {len(by_receipt)} · −{spent.normalize():f} ₽"
        account = self.store.setting("playerok:account_id")
        if (
            self.api is not None
            and account
            and self.api.account.id == account
            and all(json.loads(row["payload"]).get("account_id") == account for row in rows)
        ):
            try:
                async with asyncio.timeout(3):
                    viewer = await self.api.call("get")
                if (
                    viewer.get("id") == account
                    and self.api.account.id == account
                    and self.store.setting("playerok:account_id") == account
                ):
                    balance = (viewer.get("profile") or {}).get("balance") or {}
                    available = money(balance.get("available"))
                    checked = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%H:%M")
                    text += f" · доступно {available.normalize():f} ₽ на {checked}"
            except (APIError, TimeoutError, ValueError):
                pass
        if " · доступно " not in text:
            text += " · остаток не подтверждён"
        self.store.set_setting(key, {"ids": ids, "text": text})
        if any(
            row["id"] not in previous.get("ids", []) and row["notice_status"] == "PENDING" for row in rows
        ):
            event("🚀 ПРОДВИЖЕНИЕ", text, success=True)
        return text

    async def notify_promotions(self, bot):
        import hashlib

        from app.services.notification_preferences import NotificationPreferences

        for admin in self.store.rows("SELECT user_id FROM administrators"):
            owner = admin["user_id"]
            report = self.promotion_report(owner)
            if report is None:
                continue
            text, markup, operations, confirmed = report
            totals = await self.promotion_totals(owner, confirmed)
            if totals:
                text += "\n\n" + escaped(totals, 160)
            critical = any(
                row["status"] == "UNCERTAIN"
                or row["status"] == "PERMANENT_FAILED"
                and row["started_at"] is not None
                for row in operations
            )
            if not critical:
                try:
                    if not NotificationPreferences(self.store).enabled(owner, "promotion_guard"):
                        continue
                except ValueError:
                    continue
            fingerprint = hashlib.sha256(
                (
                    text + markup.model_dump_json() + str([(row["id"], row["status"]) for row in operations])
                ).encode()
            ).hexdigest()
            key = f"notifications:promotion-summary:{owner}"
            with self.store.transaction():
                state = self.store.setting(key, {})
                if state.get("status") == "PROCESSING" or state.get("after", 0) > time.time():
                    continue
                if state.get("status") == "UNCERTAIN" and not state.get("message_id"):
                    continue  # The first send might have arrived. Never send it again blindly.
                if state.get("fingerprint") == fingerprint:
                    continue
                state = {**state, "status": "PROCESSING"}
                self.store.set_setting(key, state)
            try:
                if state.get("message_id"):
                    message_id = state["message_id"]
                    from app.telegram.navigation_context import panel_key

                    if not hasattr(self.store, "navigation_locks"):
                        self.store.navigation_locks = {}
                    key_panel = panel_key(self.store, bot.id, owner, message_id)
                    lock = self.store.navigation_locks.setdefault((bot.id, owner, key_panel), asyncio.Lock())
                    async with lock:
                        panel = self.store.setting(panel_key(self.store, bot.id, owner, message_id), {})
                        if panel.get("route") is not None or panel.get("closed"):
                            self.store.set_setting(key, {"status": "SENT", "fingerprint": fingerprint})
                            continue  # The owner turned this notice into an interactive card.
                        await bot.edit_message_text(
                            text, chat_id=owner, message_id=message_id, reply_markup=markup
                        )
                else:
                    message = await bot.send_message(owner, text, reply_markup=markup)
                    message_id = message.message_id
            except asyncio.CancelledError:
                self.store.set_setting(key, {**state, "status": "UNCERTAIN"})
                raise
            except Exception as error:  # noqa: BLE001 -- retain durable Telegram outcome without raw RPC data
                code = telegram_error(error)
                if state.get("message_id") and code == "MESSAGE_NOT_MODIFIED":
                    message_id = state["message_id"]
                else:
                    outcome, _, delay = send_failure(error)
                    if state.get("message_id") and code in TERMINAL_EDIT:
                        state = {}  # Telegram proved this old message cannot be edited.
                    self.store.set_setting(
                        key, {**state, "status": outcome, "after": time.time() + max(delay, 5)}
                    )
                    continue
            with self.store.transaction():
                self.store.set_setting(
                    key, {"status": "SENT", "message_id": message_id, "fingerprint": fingerprint}
                )
                for row in operations:
                    self.store.execute(
                        "UPDATE operation_receipts SET notice_status='SENT',telegram_message_id=? "
                        "WHERE operation_id=? AND owner_id=? AND notice_status='PENDING'",
                        (message_id, row["id"], owner),
                    )

    async def notify(self, bot):
        await self.notify_promotions(bot)
        rows = self.store.rows(
            "WITH eligible AS (SELECT r.operation_id,r.owner_id,r.view_message_id,r.view_token,o.created_at,"
            "CASE o.status WHEN 'UNCERTAIN' THEN 1 ELSE 2 END priority FROM operation_receipts r "
            "JOIN outbox o ON o.id=r.operation_id WHERE r.notice_status='PENDING' AND r.send_retry_at<=? "
            "AND NOT (o.status='SUCCEEDED' AND o.operation_key LIKE 'chat-card-reply:%') "
            "AND NOT (o.kind='premium_bump' AND json_extract(o.payload,'$.automatic')=1) "
            "AND NOT (o.status='UNCERTAIN' AND o.kind IN ('delivery','delivery_text','gift_send','gift_notice','fulfill') "
            "AND EXISTS(SELECT 1 FROM orders d WHERE d.id=o.target)) "
            "AND NOT (o.status='PERMANENT_FAILED' AND o.kind='gift_send' AND EXISTS("
            "SELECT 1 FROM inbox i WHERE i.key='order-delivery-problem:'||o.target||':'||o.error_code "
            "AND json_extract(i.payload,'$.gift_operation')=o.id)) "
            "AND NOT (o.status='SUCCEEDED' AND (o.kind IN ('delivery','delivery_text','gift_send','gift_notice') "
            "OR o.kind='fulfill' AND EXISTS(SELECT 1 FROM inbox i WHERE i.key='order:'||o.target||':SENT'))) "
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
