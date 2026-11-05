"""Read-only evidence for UI. Execution authority remains in the existing guards."""

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime

from app.database.store import Conflict, now
from app.gifts.core import GiftValidationError, gift_reason
from app.gifts.mtproto import GiftFailure
from app.gifts.service import GiftService
from app.gifts.settings import GiftSettings
from app.playerok.adapter import APIError
from app.security.logging import event
from app.services.automation import Automation, DeliveryPolicy
from app.services.edit_confirmation import is_ordinary_edit
from app.services.inventory import Inventory
from app.services.logical_products import LogicalProducts
from app.services.playerok_connection import REASONS
from app.services.products import ValidationError
from app.services.sync_health import orders_sync_failed


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

    def observed_item(self, ident, projection=None):
        """UI evidence only; never supplies execution guards with cached authority."""
        key = (self.api.account.id, ident)
        cached = getattr(self.api, "readiness_items", {}).get(key)
        at = getattr(self.api, "readiness_item_times", {}).get(key)
        version = getattr(self.api, "readiness_item_versions", {}).get(key)
        if not cached or not fresh(at, 120) or version != self.item_version(ident):
            return projection
        if projection:
            fields = (
                "id",
                "name",
                "description",
                "price",
                "status",
                "deleted_at",
                "category",
                "obtaining_type",
                "attributes",
                "data_fields",
            )
            if any(k in projection and projection[k] != cached.get(k) for k in fields):
                return projection
            if (projection.get("user") or {}).get("id") != self.api.account.id:
                return projection
        return cached

    def item_version(self, ident):
        # An edit queued/closed after the observation invalidates UI evidence too.
        rows = self.store.rows(
            "SELECT id,status,attempts,error_code,started_at FROM outbox "
            "WHERE target=? AND kind IN ('edit','edit_fields') ORDER BY id",
            (ident,),
        )
        return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()

    def remember_item(self, item, *, version=None):
        """Record a freshly owner-checked read for consistent UI projections."""
        key = (self.api.account.id, item["id"])
        if (item.get("user") or {}).get("id") != key[0]:
            raise Conflict("Владелец наблюдаемого объявления не подтверждён")
        for name in ("readiness_items", "readiness_item_times", "readiness_item_versions"):
            if not hasattr(self.api, name):
                setattr(self.api, name, {})
        if len(self.api.readiness_items) >= 200:
            self.api.readiness_items.clear()
            self.api.readiness_item_times.clear()
            self.api.readiness_item_versions.clear()
        self.api.readiness_items[key] = deepcopy(item)
        self.api.readiness_item_times[key] = now()
        self.api.readiness_item_versions[key] = self.item_version(item["id"]) if version is None else version

    def snapshot(self, ident, *, item=None, for_arm=False):
        if not for_arm and (
            not item or any(item.get(k) is None for k in ("description", "category", "obtaining_type"))
        ):
            item = self.observed_item(ident, item)
        product = self.logical.listing(ident)
        policy_error = False
        try:
            revision, policy = self.automation.get(ident)
        except ValidationError:
            revision, policy, policy_error = 0, DeliveryPolicy(), True
        gift = self.gifts.listing(ident)
        config = json.loads(product["config"]) if product else {}
        prepared_gift = bool(
            product
            and product["handler"] == "telegram_gifts"
            and product["state"] == "VERIFIED"
            and product["verified_at"]
            and product["revision"] == product["config_revision"]
            and not product["enabled"]
            and not (gift and gift["enabled"])
        )
        mode = (
            "playerok"
            if config.get("platform_only") or policy.mode == "playerok"
            else "telegram_gifts"
            if gift or product and product["handler"] == "telegram_gifts"
            else product["handler"]
            if product
            else {"stock": "unique_stock", "text": "fixed_text"}.get(policy.mode, "manual")
        )
        unconfigured = not revision and not product and not gift
        projection_enabled = (
            bool(gift["enabled"]) if mode == "telegram_gifts" and gift else bool(policy.enabled)
        )
        owner_enabled = bool(product["enabled"]) if product else projection_enabled
        enabled = owner_enabled
        if mode == "playerok":
            enabled = enabled and (policy.paid_reply or policy.mark_sent)
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
        manager = getattr(self.api, "connection", None)
        if not getattr(account, "writes_enabled", False) and not getattr(manager, "desired_writes", False):
            add(
                "WRITES_OFF",
                "Записи Playerok выключены: PLAYEROK_WRITES_ENABLED=false в .env. Для разрешения записей измените его локально и перезапустите бота.",
                "Как разрешить записи",
                "rd:permissions:" + ident,
            )
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
        if orders_sync_failed(self.store):
            add(
                "SYNC_ERROR",
                "Последняя синхронизация завершилась ошибкой.",
                "Диагностика синхронизации",
                "diagnostics",
            )
        legacy = product and product["state"] == "LEGACY"
        migrating = product and (
            legacy or len(product["code"]) != 3 or product["error_code"] == "CODE_MIGRATING"
        )
        if migrating:
            add(
                "CODE_MIGRATION",
                "Требуется подтверждённый перенос кода в название. Новая выдача остановлена.",
                "🔐 Подтвердить новый код" if legacy else "🔄 Перенос кодов товаров",
                "lp:item:" + ident if legacy else "codes:home",
            )
        if mode != "manual" and not migrating and (not product or product["state"] != "VERIFIED"):
            add(
                "BINDING",
                "Постоянная привязка не подтверждена Playerok.",
                "Подтвердить привязку",
                "lp:item:" + ident,
            )
        listing_verified = False
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
            if product["error_code"] and not migrating:
                add(
                    "BINDING_CONFLICT",
                    "Обнаружен конфликт постоянного кода. Выдача остановлена.",
                    "Проверить привязку",
                    "lp:item:" + ident,
                    terminal=True,
                )
            complete_item = item and all(
                item.get(key) is not None
                for key in ("description", "category", "obtaining_type", "price", "name")
            )
            if item and product["state"] == "VERIFIED" and not complete_item:
                add(
                    "ITEM_UNCHECKED",
                    "Актуальность объявления ещё не подтверждена серверной проверкой.",
                    "Проверить объявление",
                    "rd:check:" + ident,
                    unknown=True,
                )
            if complete_item and product["state"] == "VERIFIED":
                try:
                    self.logical.verify_listing(product, item)
                    listing_verified = True
                except (Conflict, ValueError) as exc:
                    title_only = self.logical.title_change_allowed(product, item)
                    add(
                        "ITEM_CHANGED",
                        str(exc),
                        "Подтвердить новое название" if title_only else "Проверить объявление",
                        ("lp:title:" if title_only else "lp:item:") + ident,
                    )
            if not for_arm and owner_enabled != projection_enabled:
                add(
                    "PROJECTION_MISMATCH",
                    "Разрешения товара и способа выдачи расходятся. Подтвердите включение заново либо остановите товар.",
                    "Восстановить разрешение выдачи",
                    ("platform:item:" if mode == "playerok" else "lp:ready:") + ident,
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
            f"SELECT b.id,b.kind,b.target,b.payload,b.status,b.error_code,EXISTS(SELECT 1 FROM gift_jobs g WHERE g.order_id=b.target) AS gift_job FROM outbox b LEFT JOIN orders o ON o.id=b.target WHERE b.kind!='premium_bump' AND (o.item_id IN ({marks}) OR b.target IN ({marks})) AND (b.status IN ('UNCERTAIN','PROCESSING','PENDING','RETRYABLE_FAILED') OR b.error_code='MANUALLY_STOPPED_UNCERTAIN') ORDER BY b.created_at DESC LIMIT 10",
            ids + ids,
        )
        for op in operations:
            if (
                op["status"] == "PERMANENT_FAILED"
                and op["error_code"] == "MANUALLY_STOPPED_UNCERTAIN"
                and is_ordinary_edit(op)
            ):
                if not listing_verified:
                    add(
                        "CLOSED_EDIT_RECHECK",
                        "Запись правки закрыта. Проверьте текущую привязку товара; результат старой правки не подтверждён.",
                        "Проверить текущую привязку",
                        "rd:check:" + ident,
                        unknown=True,
                    )
                continue
            uncertain = op["status"] == "UNCERTAIN" or op["error_code"] == "MANUALLY_STOPPED_UNCERTAIN"
            route = (
                "gft:details:" + op["target"]
                if op["kind"] in {"gift_send", "gift_refund"} and op["gift_job"]
                else "ops:show:" + op["id"]
            )
            add(
                "UNCERTAIN" if uncertain else "IN_PROGRESS",
                "Результат операции неизвестен. Повтор запрещён до сверки."
                if uncertain
                else "Есть незавершённая операция. Посмотрите её состояние.",
                "Сверить без повторной отправки" if uncertain else "Посмотреть операцию",
                route,
                terminal=uncertain,
                blocking=uncertain,
                unknown=not uncertain,
            )
        if self.store.rows(
            f"SELECT 1 FROM delivery_chains WHERE item_id IN ({marks}) AND status='BLOCKED' LIMIT 1", ids
        ):
            add(
                "CHAIN_BLOCKED",
                "Одна из сделок требует проверки. Остальные заказы обрабатываются независимо.",
                "Проблемы выдачи",
                "aq:list:chains:0",
                blocking=False,
            )
        balance = self.store.setting("gifts:balance", {})
        required = reserved = shortage = None
        if mode == "telegram_gifts":
            if self.gifts.runtime_reason() == "PROXY_UNAVAILABLE":
                add(
                    "PROXY_UNAVAILABLE",
                    "Соединение Telegram восстанавливается. Новые отправки ожидают подключения.",
                    "Проверить подключение",
                    "rd:check:" + ident,
                    unknown=True,
                )
            required = (
                gift["approved_stars"]
                if gift and not prepared_gift
                else sum(g["stars"] for g in config.get("bundle", []))
            )
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
            if self.store.setting("gifts:paused", False):
                add(
                    "GIFTS_PAUSE",
                    "Подарочные покупки временно приостановлены. Настройки товаров сохранены.",
                    "Снять временную паузу",
                    "gft:settings",
                )
            if self.store.rows(
                "SELECT 1 FROM gift_jobs WHERE state='UNCERTAIN' OR refund_state='UNCERTAIN' LIMIT 1"
            ):
                add(
                    "GIFT_UNCERTAIN",
                    "Есть неизвестный результат платежа. Новые покупки заблокированы до сверки.",
                    "Проверить подарочные операции",
                    "gft:list:all:0",
                    terminal=True,
                )
            elif self.store.rows("SELECT 1 FROM gift_jobs WHERE state='SPENDING' LIMIT 1"):
                add(
                    "GIFT_IN_PROGRESS",
                    "Отправляем подарок. Следующие покупки ожидают завершения текущей отправки.",
                    "Посмотреть отправку",
                    "gft:list:all:0",
                    unknown=True,
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
            expected = json.loads(gift["bundle"]) if gift and not prepared_gift else config.get("bundle", [])
            try:
                bundle = self.gifts.catalog_bundle(
                    gift["gift_code"] if gift and not prepared_gift else config["code"]
                )
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
            daily_exhausted = day_limit is not None and spent + reserved + required > day_limit
            if required > order_limit or daily_exhausted:
                global_order, global_day = self.gifts.limits()
                local_limit = (
                    required > order_limit
                    and order_limit < global_order
                    or (daily_exhausted and day_limit < global_day)
                )
                detail = f"Лимит Stars: {order_limit} на заказ. Пакет: {required}."
                if daily_exhausted:
                    detail += (
                        f" Потолок суток: {day_limit}, расходы и резервы: {spent + reserved}. "
                        "Выдача продолжится после сброса лимита в 00:00 UTC при достаточном балансе."
                    )
                add(
                    "STARS_LIMIT",
                    detail,
                    "Лимиты этого товара" if local_limit else "Общие лимиты подарков",
                    "lp:limits:" + ident if local_limit and product else "gft:limits",
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
            complete = policy.mark_sent and (mode == "playerok" or policy.automatic_completion)
        measurement = self.store.setting("readiness:" + ident, {})
        completion_note = None
        if mode in {"fixed_text", "unique_stock"} and policy.mark_sent and not policy.automatic_completion:
            completion_note = "вручную; напоминание включено"
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
        if unconfigured:
            mode, state = "unconfigured", "UNKNOWN"
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

    async def check(self, owner, ident, *, for_arm=False, automatic=False):
        self.logical.authority(owner)
        locks = getattr(self.api, "readiness_locks", None)
        if locks is None:
            self.api.readiness_locks = locks = {}
        lock = locks.setdefault((self.api.account.id, ident), asyncio.Lock())
        old = self.store.setting("readiness:" + ident, {})
        cached_items = getattr(self.api, "readiness_items", None)
        if cached_items is None:
            self.api.readiness_items = cached_items = {}
        cached_times = getattr(self.api, "readiness_item_times", None)
        if cached_times is None:
            self.api.readiness_item_times = cached_times = {}
        cached_versions = getattr(self.api, "readiness_item_versions", None)
        if cached_versions is None:
            self.api.readiness_item_versions = cached_versions = {}
        cache_key = (self.api.account.id, ident)
        version = self.item_version(ident)
        changed = old.get("edit_version", cached_versions.get(cache_key)) != version
        if not for_arm and (lock.locked() or fresh(old.get("attempt"), 30) and not changed):
            return {**self.snapshot(ident), "busy": True}
        async with lock:
            item, error = None, None
            self.store.set_setting("readiness:" + ident, {**old, "attempt": now(), "edit_version": version})
            try:
                async with asyncio.timeout(30):
                    item = await self.automation.item(owner, ident)
                    self.remember_item(item, version=version)
                    product = self.logical.listing(ident)
                    if product and product["error_code"] == "RELIST_CONFIRMATION_REQUIRED":
                        from app.workers.logical_products import LogicalProductWorker

                        # Clear an old scan blocker only through a complete
                        # catalogue/duplicate check and a second server read.
                        self.logical.verify_listing(product, item)
                        await LogicalProductWorker(self.store, self.api).scan(product_id=product["id"])
                    _, policy = self.automation.get(ident)
                    gift = self.gifts.listing(ident)
                    if (
                        gift or product and product["handler"] == "telegram_gifts"
                    ) and policy.mode != "playerok":
                        gift_lock = getattr(self.api, "readiness_gift_lock", None)
                        if gift_lock is None:
                            self.api.readiness_gift_lock = gift_lock = asyncio.Lock()
                        async with gift_lock:
                            if (
                                not automatic
                                or not fresh(self.store.setting("gifts:balance", {}).get("at"), 60)
                                or not fresh(self.store.setting("gifts:connection", {}).get("at"), 60)
                            ):
                                await self.gifts.refresh()
                        if item.get("status") == "APPROVED":
                            code = gift["gift_code"] if gift else json.loads(product["config"])["code"]
                            await self.gifts.inspect_item(owner, ident, preview_code=code, observed_item=item)
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
            except Exception as exc:
                self.store.set_setting(
                    "readiness:" + ident,
                    {
                        **self.store.setting("readiness:" + ident, {}),
                        "error": "Внутренняя ошибка проверки. Готовность не подтверждена; откройте диагностику и передайте разработчику время ошибки.",
                        "success": old.get("success"),
                    },
                )
                event(
                    "ERROR", f"Readiness stage=check type={type(exc).__name__} code=INTERNAL_ERROR", level=40
                )
                raise
            record = self.store.setting("readiness:" + ident, {})
            self.store.set_setting(
                "readiness:" + ident,
                {**record, "error": error, "success": old.get("success") if error else now()},
            )
            return self.snapshot(ident, item=item, for_arm=for_arm)

    async def refresh_due(self):
        """Bounded read-only batch, oldest attempt first; no execution authority."""
        account = self.api.account
        if (
            not account.id
            or account.id != self.store.setting("playerok:account_id")
            or getattr(account, "connection_verified", True) is not True
        ):
            return 0
        candidates = self.store.rows(
            "SELECT l.item_id,p.owner_id FROM logical_listings l JOIN logical_products p ON p.id=l.product_id "
            "JOIN administrators a ON a.user_id=p.owner_id "
            "WHERE p.account_id=? AND p.enabled=1 AND p.state='VERIFIED' "
            "AND l.source_order_id IS NULL AND l.server_status='APPROVED'",
            (account.id,),
        )
        candidates.sort(
            key=lambda row: self.store.setting("readiness:" + row["item_id"], {}).get("attempt", "")
        )
        due = [
            row
            for row in candidates
            if not fresh(self.store.setting("readiness:" + row["item_id"], {}).get("attempt"), 120)
        ][:4]
        slots = asyncio.Semaphore(2)

        async def check(row):
            async with slots:
                await self.check(row["owner_id"], row["item_id"], automatic=True)

        await asyncio.gather(*(check(row) for row in due))
        return len(due)

    async def run(self, stop):
        from app.workers.supervisor import worker_cycle

        while not stop.is_set():
            with worker_cycle(self.store, "readiness", 30, max_seconds=75) as cycle:
                cycle["processed"] = await self.refresh_due()
            try:
                await asyncio.wait_for(stop.wait(), 15)
            except TimeoutError:
                pass
