"""Read-only evidence for UI. Execution authority remains in the existing guards."""

import asyncio
import json
from datetime import UTC, datetime

from app.database.store import Conflict, now
from app.gifts.core import GiftValidationError, gift_reason
from app.gifts.mtproto import GiftFailure
from app.gifts.service import GiftService
from app.gifts.settings import GiftSettings
from app.playerok.adapter import APIError
from app.security.logging import event
from app.services.automation import Automation, DeliveryPolicy
from app.services.inventory import Inventory
from app.services.logical_products import LogicalProducts
from app.services.playerok_connection import REASONS
from app.services.products import ValidationError


def fresh(value, seconds=300):
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(value)).total_seconds()
        return 0 <= age <= seconds
    except (TypeError, ValueError, AttributeError):
        return False


class ProductReadiness:
    def __init__(self, store, api, sender=None):
        self.store, self.api = store, api
        self.logical = LogicalProducts(store, api)
        self.automation = Automation(store, api)
        self.gifts = GiftService(
            store,
            api,
            getattr(api, "gift_settings", None) or GiftSettings.load(),
            sender or getattr(api, "gift_sender", None),
        )

    def snapshot(self, ident, *, item=None, for_arm=False):
        product = self.logical.listing(ident)
        policy_error = False
        try:
            _, policy = self.automation.get(ident)
        except ValidationError:
            policy, policy_error = DeliveryPolicy(), True
        gift = self.gifts.listing(ident)
        config = json.loads(product["config"]) if product else {}
        mode = (
            "playerok"
            if config.get("platform_only") or policy.mode == "playerok"
            else "telegram_gifts"
            if gift or product and product["handler"] == "telegram_gifts"
            else product["handler"]
            if product
            else {"stock": "unique_stock", "text": "fixed_text"}.get(policy.mode, "manual")
        )
        enabled = bool(gift["enabled"]) if mode == "telegram_gifts" and gift else bool(policy.enabled)
        problems = []

        def add(code, text, label, route, *, unknown=False, terminal=False, blocking=True):
            if not any(p["code"] == code for p in problems):
                problems.append(
                    {
                        "code": code,
                        "text": text,
                        "action": (label, route),
                        "unknown": unknown,
                        "terminal": terminal,
                        "blocking": blocking,
                        "manual": terminal,
                    }
                )

        account = self.api.account
        if policy_error:
            add(
                "POLICY_INVALID",
                "Сохранённые настройки выдачи неполны. Откройте настройки товара и сохраните их заново.",
                "Исправить настройки",
                "iv:delivery:" + ident,
            )
        if not getattr(account, "id", None) or account.id != self.store.setting("playerok:account_id"):
            add(
                "ACCOUNT",
                "Аккаунт Playerok не соответствует привязке базы. Продажи ботом не обрабатываются.",
                "Проверить Playerok",
                "profile:check",
                terminal=True,
            )
        if getattr(account, "connection_verified", True) is not True:
            add(
                "CONNECTION",
                "Playerok не подтверждён. Проверьте подключение.",
                "Проверить Playerok",
                "profile:check",
                unknown=True,
            )
        if not getattr(account, "writes_enabled", False):
            add("WRITES_OFF", "Записи Playerok выключены в настройках запуска.", "Подключения", "profile")
        if not getattr(account, "automation_enabled", False):
            add(
                "AUTOMATION_OFF",
                "Глобальная автоматизация выключена.",
                "Управление автоматизацией",
                "automation",
            )
        if not self.store.setting("orders:last_complete_sync"):
            add(
                "SYNC_MISSING",
                "Первичная синхронизация заказов ещё не завершена.",
                "Проверить синхронизацию",
                "diagnostics",
                unknown=True,
            )
        elif not fresh(self.store.setting("orders:last_complete_sync"), 600):
            add(
                "SYNC_STALE",
                "Сведения о заказах устарели. Нужна свежая синхронизация.",
                "Проверить синхронизацию",
                "diagnostics",
                unknown=True,
            )
        if self.store.setting("sync:health", {}).get("status") == "ERROR":
            add(
                "SYNC_ERROR",
                "Последняя синхронизация завершилась ошибкой.",
                "Диагностика синхронизации",
                "diagnostics",
            )
        if mode != "manual" and (not product or product["state"] != "VERIFIED"):
            add(
                "BINDING",
                "Постоянная привязка не подтверждена Playerok.",
                "Подтвердить привязку",
                "lp:item:" + ident,
            )
        if product:
            if (mode == "playerok" and gift and gift["enabled"]) or (
                mode != "manual"
                and not (mode == "playerok" and config.get("platform_only"))
                and product["handler"] != mode
            ):
                add(
                    "MODE_CONFLICT",
                    "Настройки способов выдачи конфликтуют. Новую выдачу включать нельзя до проверки.",
                    "Исправить способ выдачи",
                    "iv:delivery:" + ident,
                    terminal=True,
                )
            if product["error_code"]:
                add(
                    "BINDING_CONFLICT",
                    "Обнаружен конфликт постоянного кода. Выдача остановлена.",
                    "Проверить привязку",
                    "lp:item:" + ident,
                    terminal=True,
                )
            if item and product["state"] == "VERIFIED":
                try:
                    self.logical.verify_listing(product, item)
                except (Conflict, ValueError):
                    add(
                        "ITEM_CHANGED",
                        "Описание или параметры товара изменились. Подтвердите привязку заново.",
                        "Проверить объявление",
                        "lp:item:" + ident,
                    )
            if enabled and not product["enabled"] and not for_arm:
                add(
                    "PRODUCT_STOP",
                    "Выдача остановлена в настройках товара.",
                    "Настроить выдачу",
                    "iv:delivery:" + ident,
                )
        if item and (item.get("status") and item.get("status") != "APPROVED" or item.get("deleted_at")):
            add(
                "NOT_PUBLISHED",
                "Товар сейчас не опубликован. Настройки сохраняются для будущих продаж.",
                "Открыть товар",
                "item:" + ident,
            )
        if not item:
            add(
                "ITEM_UNCHECKED",
                "Актуальное состояние объявления не проверено.",
                "Проверить объявление",
                "rd:check:" + ident,
                unknown=True,
            )
        if enabled and mode not in {"telegram_gifts", "manual"} and not policy.auto_enroll:
            add(
                "FUTURE_ORDERS_OFF",
                "Обработка новых покупок не разрешена настройками товара.",
                "Настроить новые покупки",
                "lp:item:" + ident,
            )
        if enabled and not for_arm and self.store.setting("delivery:" + ident + ":enabled") is False:
            add(
                "LOCAL_STOP",
                "Действует остановка отправок этого товара.",
                "Управление выдачей",
                "iv:delivery:" + ident,
                terminal=True,
            )
        ids = [ident]
        if product:
            ids = [
                r["item_id"]
                for r in self.store.rows(
                    "SELECT item_id FROM logical_listings WHERE product_id=?", (product["id"],)
                )
            ]
        marks = ",".join("?" for _ in ids)
        operations = self.store.rows(
            f"SELECT b.id,b.status,b.error_code FROM outbox b LEFT JOIN orders o ON o.id=b.target WHERE (o.item_id IN ({marks}) OR b.target IN ({marks})) AND (b.status IN ('UNCERTAIN','PROCESSING','PENDING','RETRYABLE_FAILED') OR b.error_code='MANUALLY_STOPPED_UNCERTAIN') ORDER BY b.created_at DESC LIMIT 10",
            ids + ids,
        )
        for op in operations:
            uncertain = op["status"] == "UNCERTAIN" or op["error_code"] == "MANUALLY_STOPPED_UNCERTAIN"
            add(
                "UNCERTAIN" if uncertain else "IN_PROGRESS",
                "Результат операции неизвестен. Повтор запрещён до сверки."
                if uncertain
                else "Есть незавершённая операция. Посмотрите её состояние.",
                "Сверить без повторной отправки" if uncertain else "Посмотреть операцию",
                "ops:show:" + op["id"],
                terminal=uncertain,
                blocking=uncertain,
            )
        if self.store.rows(
            f"SELECT 1 FROM delivery_chains WHERE item_id IN ({marks}) AND status='BLOCKED' LIMIT 1", ids
        ):
            add(
                "CHAIN_BLOCKED",
                "Есть остановленная выдача. Сначала разрешите проблемный заказ.",
                "Проблемы выдачи",
                "aq:list:chains:0",
                terminal=True,
            )
        balance = self.store.setting("gifts:balance", {})
        required = reserved = shortage = None
        if mode == "telegram_gifts":
            required = gift["approved_stars"] if gift else sum(g["stars"] for g in config.get("bundle", []))
            connection = self.store.setting("gifts:connection", {})
            if connection.get("status") != "OK" or not fresh(connection.get("at")):
                add(
                    "MTPROTO",
                    "Личный Telegram не подтверждён свежей проверкой.",
                    "Проверить Telegram",
                    "rd:check:" + ident,
                    unknown=True,
                )
            if not fresh(balance.get("at")):
                add(
                    "BALANCE_STALE",
                    "Баланс Stars устарел или ещё не проверен.",
                    "Обновить баланс",
                    "rd:balance:" + ident,
                    unknown=True,
                )
            if self.store.rows("SELECT 1 FROM gift_steps WHERE spent_at>? LIMIT 1", (balance.get("at", ""),)):
                add(
                    "BALANCE_SPENT",
                    "После замера баланса были расходы. Обновите Stars.",
                    "Обновить баланс",
                    "rd:balance:" + ident,
                    unknown=True,
                )
            if not self.gifts.settings.payment_enabled:
                add(
                    "PAYMENTS_OFF",
                    "Покупки подарков выключены в настройках запуска.",
                    "Настройки подарков",
                    "gft:settings",
                )
            if self.store.setting("gifts:stopped", True):
                add(
                    "GIFTS_STOP",
                    "Действует общая остановка подарочных покупок.",
                    "Управление подарками",
                    "gft:settings",
                )
            if self.store.rows(
                "SELECT 1 FROM gift_jobs WHERE state IN ('SPENDING','UNCERTAIN') OR refund_state='UNCERTAIN' LIMIT 1"
            ):
                add(
                    "GIFT_UNCERTAIN",
                    "Есть начатый платёж или неизвестный результат. Новые покупки заблокированы.",
                    "Проверить подарочные операции",
                    "gft:list:all:0",
                    terminal=True,
                )
            reserved = self.store.rows("SELECT COALESCE(SUM(reserved_stars),0) AS n FROM gift_jobs")[0]["n"]
            if type(balance.get("stars")) is int:
                shortage = max(0, required + reserved - balance["stars"])
                if shortage:
                    add(
                        "STARS_SHORTAGE",
                        f"Недостаточно Stars: не хватает {shortage} ⭐ с учётом резервов.",
                        "Обновить баланс",
                        "rd:balance:" + ident,
                    )
            expected = json.loads(gift["bundle"]) if gift else config.get("bundle", [])
            try:
                bundle = self.gifts.catalog_bundle(gift["gift_code"] if gift else config["code"])
                if bundle != expected:
                    add(
                        "GIFT_PRICE",
                        "Цена или состав подарков изменились. Повторно согласуйте привязку.",
                        "Проверить состав и цену",
                        "lp:item:" + ident,
                    )
                codes = [g["code"] for g in expected]
                if any(
                    not fresh(r["checked_at"])
                    for r in self.store.rows("SELECT code,checked_at FROM gift_catalog")
                    if r["code"] in codes
                ):
                    add(
                        "CATALOG_STALE",
                        "Каталог подарков устарел. Доступность и цена требуют проверки.",
                        "Обновить каталог",
                        "rd:check:" + ident,
                        unknown=True,
                    )
            except (ValueError, GiftFailure, KeyError):
                add(
                    "GIFT_UNAVAILABLE",
                    "Подарок отсутствует в проверенном каталоге или недоступен.",
                    "Проверить подарок",
                    "rd:check:" + ident,
                )
            order_limit, day_limit = self.gifts.limits(ident)
            spent = self.store.rows(
                "SELECT COALESCE(SUM(stars),0) AS n FROM gift_steps WHERE spent_at>=?",
                (datetime.now(UTC).date().isoformat(),),
            )[0]["n"]
            if required > order_limit or spent + reserved + required > day_limit:
                add(
                    "STARS_LIMIT",
                    "Пакет превышает доступный лимит Stars на заказ или сутки.",
                    "Настроить лимиты",
                    "gft:limits",
                )
            after = self.store.setting("gifts:listing_options:" + ident, {}).get("after")
            message = (
                "Своё сообщение после подтверждённой доставки"
                if after
                else "Стандартное уведомление после подтверждённой доставки"
            )
            complete = bool(gift and gift["auto_fulfill"])
        else:
            if mode == "unique_stock" and not Inventory(self.store, self.api).counts(ident)["AVAILABLE"]:
                add(
                    "STOCK_EMPTY", "Нет доступных единиц на складе.", "Пополнить склад", "stock:view:" + ident
                )
            if mode == "fixed_text" and not (config.get("text") or policy.text):
                add("TEXT_MISSING", "Текст выдачи не задан.", "Задать текст", "lp:item:" + ident)
            message = (
                ("После оплаты: включён" if policy.paid_reply else "После оплаты: выключен")
                if mode == "playerok"
                else ("Своё сообщение после выдачи" if policy.after else "После выдачи: выключено")
            )
            complete = policy.mark_sent
        measurement = self.store.setting("readiness:" + ident, {})
        completion_note = None
        if complete and item and item.get("seller_type") == "SYSTEM":
            completion_note = "Отметку выполнения делает Playerok: системный товар. Выдача и сообщения проверяются независимо."
            add(
                "COMPLETION_UNAVAILABLE",
                completion_note,
                "Настроить завершение",
                "iv:completion:" + ident,
                blocking=False,
            )
        if measurement.get("error"):
            add("CHECK_FAILED", measurement["error"], "Проверить ещё раз", "rd:check:" + ident, unknown=True)
        blocking = [p for p in problems if p["blocking"]]
        if mode == "manual":
            problems = [p for p in problems if p["terminal"] or p["code"] == "IN_PROGRESS"]
            blocking = [p for p in problems if p["blocking"]]
        state = (
            "STOPPED"
            if any(p["terminal"] for p in blocking)
            else "OFF"
            if not enabled and not for_arm
            else "BLOCKED"
            if any(not p["unknown"] for p in blocking)
            else "UNKNOWN"
            if blocking
            else "READY"
        )
        return {
            "item_id": ident,
            "product_id": product["id"] if product else None,
            "mode": mode,
            "binding": product["state"] if product else None,
            "code": product["code"] if product else None,
            "enabled": enabled,
            "state": state,
            "ready": state == "READY",
            "problems": problems,
            "message": message,
            "complete": complete,
            "completion_note": completion_note,
            "additional_ready": "CHECK_ORDER" if complete else "OFF",
            "stock": Inventory(self.store, self.api).counts(ident) if mode == "unique_stock" else None,
            "balance": balance,
            "required": required,
            "reserved": reserved,
            "shortage": shortage,
            "checked_at": measurement.get("success"),
            "attempted_at": measurement.get("attempt"),
            "sources": ["Playerok", "SQLite", "MTProto" if mode == "telegram_gifts" else "Inventory"],
            "manual": any(p["manual"] for p in problems),
        }

    async def check(self, owner, ident, *, for_arm=False):
        self.logical.authority(owner)
        locks = getattr(self.api, "readiness_locks", None)
        if locks is None:
            self.api.readiness_locks = locks = {}
        lock = locks.setdefault(ident, asyncio.Lock())
        old = self.store.setting("readiness:" + ident, {})
        cached_items = getattr(self.api, "readiness_items", None)
        if cached_items is None:
            self.api.readiness_items = cached_items = {}
        cache_key = (self.api.account.id, ident)
        if not for_arm and (lock.locked() or fresh(old.get("attempt"), 3)):
            return {**self.snapshot(ident, item=cached_items.get(cache_key), for_arm=for_arm), "busy": True}
        async with lock:
            item, error = None, None
            self.store.set_setting("readiness:" + ident, {**old, "attempt": now()})
            try:
                async with asyncio.timeout(30):
                    item = await self.automation.item(owner, ident)
                    if len(cached_items) >= 200:
                        cached_items.clear()
                    cached_items[cache_key] = item
                    product = self.logical.listing(ident)
                    _, policy = self.automation.get(ident)
                    gift = self.gifts.listing(ident)
                    if (
                        gift or product and product["handler"] == "telegram_gifts"
                    ) and policy.mode != "playerok":
                        await self.gifts.refresh()
                        if item.get("status") == "APPROVED":
                            code = gift["gift_code"] if gift else json.loads(product["config"])["code"]
                            await self.gifts.inspect_item(owner, ident, preview_code=code)
            except (
                APIError,
                GiftFailure,
                TimeoutError,
                ConnectionError,
                Conflict,
                ValidationError,
                GiftValidationError,
            ) as exc:
                code = getattr(exc, "code", "CHECK_FAILED")
                safe_code = code if code in REASONS else "CHECK_FAILED"
                event("WARNING", f"Readiness stage=check type={type(exc).__name__} code={safe_code}")
                error = "Проверка не завершена: " + (
                    {
                        "SESSION_UNAUTHORIZED": "Личный Telegram не авторизован. Выполните локальную авторизацию аккаунта.",
                        "SESSION_NOT_CONFIGURED": "Личный Telegram не настроен. Проверьте настройки подключения.",
                        "PERSONAL_ACCOUNT_CHANGED": "Личный Telegram отличается от привязанного аккаунта. Покупки запрещены.",
                        "MTPROTO_TIMEOUT": "Личный Telegram не ответил вовремя. Проверьте прокси и повторите read-only проверку.",
                    }.get(code, gift_reason(code))
                    if isinstance(exc, GiftFailure)
                    else "параметры товара или принадлежность аккаунта не подтверждены. Откройте товар и проверьте привязку."
                    if isinstance(exc, (Conflict, ValidationError, GiftValidationError))
                    else REASONS.get(
                        code,
                        "сервер или подключение не подтвердили данные. Сохранённые замеры не считаются свежим подтверждением.",
                    )
                )
            record = self.store.setting("readiness:" + ident, {})
            self.store.set_setting(
                "readiness:" + ident,
                {**record, "error": error, "success": old.get("success") if error else now()},
            )
            return self.snapshot(ident, item=item, for_arm=for_arm)
