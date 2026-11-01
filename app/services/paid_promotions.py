"""Paid placement through existing Outbox, budgets and exact server payment receipts."""

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.database.store import Conflict, now
from app.playerok.adapter import APIError
from app.services.finance import money, quote_fresh
from app.services.products import ValidationError, priority_price_basis
from app.services.promotion_settings import PromotionSettings
from app.services.promotions import dry_run

KINDS = {"premium_publish", "premium_bump"}


def intent_key(operation):
    return "promotions:intent:" + operation


def finish_expense(db, operation, status):
    row = db.execute("SELECT operation_key FROM outbox WHERE id=?", (operation,)).fetchone()
    if row:
        db.execute("UPDATE promotion_expenses SET status=? WHERE operation_key=?", (status, row[0]))


class PaidPromotions:
    def __init__(self, store, api):
        self.store, self.api = store, api
        self.settings = PromotionSettings(store, api)

    def permissions(self, account_id=None, automatic=False):
        a = self.api.account
        if not a.id or (account_id and account_id != a.id):
            raise ValidationError("Аккаунт Playerok изменился; платная операция заблокирована")
        if not getattr(a, "writes_enabled", False):
            raise ValidationError("Записи Playerok выключены или аккаунт не подтверждён")
        if (
            getattr(a, "connection_verified", True) is not True
            or getattr(a, "verified_account_id", a.id) != a.id
        ):
            raise ValidationError("Аккаунт Playerok не подтверждён")
        if automatic and not getattr(a, "automation_enabled", False):
            raise ValidationError("Автоматизация Playerok выключена")

    @staticmethod
    def snapshot(item):
        return {k: item.get(k) for k in ("id", "status", "price", "raw_price", "priority")}

    async def preview(self, owner, ident, quote_id):
        item = await self.settings.access.item(owner, ident)
        self.permissions()
        if item.get("status") not in {"DRAFT", "APPROVED"}:
            raise ValidationError("Платное размещение доступно для черновика или опубликованного товара")
        quotes = await self.api.call(
            "get_item_priority_statuses", item_id=ident, item_price=priority_price_basis(item)
        )
        choices = [q for q in quotes if q.get("id") == quote_id and q.get("type") == "PREMIUM"]
        if len(choices) != 1 or choices[0].get("price") is None or money(choices[0]["price"]) <= 0:
            raise ValidationError("Актуальный платный тариф PREMIUM не подтверждён")
        q = choices[0]
        revision, _, _ = self.settings.get()
        return {
            "account_id": self.api.account.id,
            "item": self.snapshot(item),
            "name": str(item.get("name") or "Товар")[:200],
            "quote_id": q["id"],
            "amount": str(money(q["price"])),
            "type": "PREMIUM",
            "period": q.get("period"),
            "policy_revision": revision,
            "checked_at": now(),
            "before_payment_id": (item.get("status_payment") or {}).get("id"),
        }

    def authority(self, value):
        revision, policy, selection = self.settings.get()
        approval = self.store.setting("promotions:approved:" + value["item"]["id"], {})
        if (
            not policy.enabled
            or revision != value["policy_revision"]
            or not self.has_authority(value["item"]["id"], revision)
            or not self.settings.selected(value["item"]["id"], selection)
            or any(
                approval.get(k) != value.get(k)
                for k in ("account_id", "quote_id", "amount", "type", "period", "policy_revision")
            )
        ):
            raise ValidationError("Платное автоподнятие не разрешено для текущей версии настроек и тарифа")
        return policy

    def has_authority(self, item, revision):
        """A launch grants its own item only; explicit shop-wide arm stays valid."""
        return any(
            permission.get("revision") == revision and permission.get("account_id") == self.api.account.id
            for permission in (
                self.store.setting("promotions:paid_authority", {}),
                self.store.setting("promotions:launch_authority:" + item, {}),
            )
        )

    def arm(self, owner, revision):
        self.settings.access.orders.owner(owner)
        self.permissions(automatic=True)
        current, policy, _ = self.settings.get()
        if (
            revision != current
            or not policy.enabled
            or min(money(policy.per_operation), money(policy.daily), money(policy.monthly)) <= 0
            or policy.max_operations_per_item < 1
        ):
            raise ValidationError(
                "Сохраните включённую политику и ненулевые лимиты перед разрешением расходов"
            )
        self.store.set_setting(
            "promotions:paid_authority",
            {"account_id": self.api.account.id, "revision": current, "actor": owner},
        )
        self.store.audit(owner, "ARM_PAID_PROMOTIONS", "*", "EXPLICIT_OWNER_PERMISSION")

    async def approve(self, owner, value, *, prepublication=False):
        fresh = await self.preview(owner, value["item"]["id"], value["quote_id"])
        if any(
            fresh.get(k) != value.get(k)
            for k in ("account_id", "item", "quote_id", "amount", "period", "policy_revision")
        ):
            raise Conflict("Тариф или товар изменился. Откройте новое превью")
        allowed = (
            fresh["item"]["status"] == "DRAFT"
            if prepublication
            else fresh["item"]["status"] == "APPROVED" and fresh["item"]["priority"] == "PREMIUM"
        )
        if not allowed:
            raise ValidationError("Для автоподнятия сначала опубликуйте или повысьте товар до PREMIUM")
        self.store.set_setting("promotions:approved:" + fresh["item"]["id"], fresh)
        self.store.audit(owner, "APPROVE_PREMIUM_TARIFF", fresh["item"]["id"], "EXPLICIT_OWNER_PERMISSION")

    async def queue(self, owner, value, key, *, automatic=False, launch=None, manual=False):
        if manual and (automatic or launch):
            raise ValidationError("Разовое разрешение не включает расписание или составной запуск")
        existing = self.store.rows("SELECT id FROM outbox WHERE operation_key=?", (key,))
        if existing:
            return existing[0]["id"]
        fresh = await self.preview(owner, value["item"]["id"], value["quote_id"])
        if any(
            fresh.get(k) != value.get(k)
            for k in (
                "account_id",
                "item",
                "quote_id",
                "amount",
                "period",
                "policy_revision",
                "before_payment_id",
            )
        ):
            raise Conflict("Тариф, товар или настройки изменились. Подтвердите новое превью")
        self.permissions(fresh["account_id"], automatic)
        viewer = await self.api.call("get")
        if viewer.get("id") != fresh["account_id"]:
            raise ValidationError("Аккаунт баланса не соответствует товару")
        available = (viewer.get("profile") or {}).get("balance", {}).get("available")
        if available is None:
            raise ValidationError("Доступный баланс Playerok не подтверждён")
        with self.store.transaction() as db:
            again = db.execute("SELECT id FROM outbox WHERE operation_key=?", (key,)).fetchone()
            if again:
                return again[0]
            revision, policy, _ = self.settings.get()
            if revision != fresh["policy_revision"]:
                raise Conflict("Лимиты изменились; откройте превью заново")
            if automatic:
                self.authority(fresh)
            if not manual:
                checked = dry_run(
                    self.store,
                    fresh["item"]["id"],
                    {"price": fresh["amount"], "checked_at": fresh["checked_at"]},
                    replace(policy, enabled=True),
                    datetime.now(UTC),
                )
                if checked["reasons"]:
                    raise ValidationError("Расход не разрешён: " + "; ".join(checked["reasons"]))
            if self.store.rows("SELECT 1 FROM promotion_expenses WHERE status='UNCERTAIN' LIMIT 1"):
                raise Conflict("Предыдущий расход Playerok неизвестен. Сначала выполните сверку")
            reserved = sum(
                (
                    money(r["amount"])
                    for r in self.store.rows("SELECT amount FROM promotion_expenses WHERE status='RESERVED'")
                ),
                Decimal(0),
            )
            if money(available) < reserved + money(fresh["amount"]):
                raise ValidationError("Недостаточно доступного баланса Playerok с учётом резервов")
            kind = "premium_publish" if fresh["item"]["status"] == "DRAFT" else "premium_bump"
            payload = {**fresh, "actor": owner, "automatic": automatic}
            if manual:
                payload["manual_authority"] = {
                    "owner": owner,
                    "limit": fresh["amount"],
                    "item": fresh["item"]["id"],
                    "quote": fresh["quote_id"],
                    "account": fresh["account_id"],
                    "key": key,
                    "revision": fresh["policy_revision"],
                }
            if launch:
                payload.update(launch_id=launch["id"], launch_revision=launch["revision"])
            op = self.store.enqueue(key, kind, fresh["item"]["id"], payload, db, actor=owner)
            if launch:
                self.store.set_setting("launch:prepared:" + fresh["item"]["id"], {**launch, "operation": op})
            db.execute(
                "INSERT INTO promotion_expenses VALUES(?,?,?,'RESERVED',?)",
                (key, fresh["item"]["id"], fresh["amount"], now()),
            )
            return op

    def guard(self, action):
        value = action["payload"]
        self.permissions(value["account_id"], value["automatic"])
        self.settings.access.orders.owner(value["actor"])
        revision, _, _ = self.settings.get()
        if revision != value["policy_revision"]:
            raise ValidationError("Настройки расходов изменились; платёж остановлен")
        if "manual_authority" in value and (
            value["automatic"]
            or value.get("launch_id")
            or self.store.rows(
                "SELECT owner_id FROM operation_receipts WHERE operation_id=?", (action["id"],)
            )
            != [{"owner_id": value["actor"]}]
            or value["manual_authority"]
            != {
                "owner": value["actor"],
                "limit": value["amount"],
                "item": action["target"],
                "quote": value["quote_id"],
                "account": value["account_id"],
                "key": action["operation_key"],
                "revision": value["policy_revision"],
            }
        ):
            raise ValidationError("Разовое согласие на PREMIUM не соответствует платежу")
        if value["automatic"]:
            self.authority(value)
        rows = self.store.rows(
            "SELECT status,lease_token,lease_until FROM outbox WHERE id=?", (action["id"],)
        )
        if (
            not rows
            or rows[0]["status"] != "PROCESSING"
            or rows[0]["lease_token"] != action["lease_token"]
            or (rows[0]["lease_until"] or 0) <= time.time()
        ):
            raise ValidationError("Платёж потерял lease; отправка остановлена")
        reservation = self.store.rows(
            "SELECT amount,status FROM promotion_expenses WHERE operation_key=?", (action["operation_key"],)
        )
        if (
            not reservation
            or reservation[0]["status"] != "RESERVED"
            or money(reservation[0]["amount"]) != money(value["amount"])
            or value["item"]["id"] != action["target"]
        ):
            raise ValidationError("Резерв платного размещения не соответствует операции")
        if self.store.rows("SELECT 1 FROM promotion_expenses WHERE status='UNCERTAIN'"):
            raise ValidationError("Есть расход с неизвестным результатом")
        for row in self.store.rows("SELECT key,value FROM settings WHERE key LIKE 'promotions:intent:%'"):
            if row["key"] != intent_key(action["id"]) and json.loads(row["value"]).get("pending"):
                raise ValidationError("Другой платёж Playerok выполняется или требует проверки")
        intent = self.store.setting(intent_key(action["id"]), {})
        if intent.get("checked_at") and not quote_fresh(intent["checked_at"], max_age=30):
            raise ValidationError("Тариф устарел до отправки. Нужно новое превью")

    def receipt(self, value, item, expected_payment):
        payment = item.get("status_payment") or {}
        if (
            not expected_payment
            or payment.get("id") != expected_payment
            or expected_payment == value.get("before_payment_id")
            or item.get("id") != value["item"]["id"]
            or (item.get("user") or {}).get("id") != value["account_id"]
            or item.get("priority") != "PREMIUM"
            or item.get("status") not in {"APPROVED", "PENDING_APPROVAL", "PENDING_MODERATION"}
            or payment.get("status") != "CONFIRMED"
            or payment.get("direction") != "OUT"
            or payment.get("operation") != "ITEM_PREMIUM_PRIORITY"
            or payment.get("provider_id") != "LOCAL"
            or payment.get("value") is None
            or money(payment["value"]) != money(value["amount"])
        ):
            raise APIError("PREMIUM_PAYMENT_NOT_CONFIRMED", uncertain=True)
        for row in self.store.rows("SELECT key,value FROM settings WHERE key LIKE 'promotions:intent:%'"):
            prior = json.loads(row["value"])
            if prior.get("receipt_id") == expected_payment and prior.get("operation_id") != value.get(
                "operation_id"
            ):
                raise APIError("PREMIUM_RECEIPT_ALREADY_USED", uncertain=True)
        return {
            "remote_id": item["id"],
            "name": item.get("name"),
            "status": item["status"],
            "priority": "PREMIUM",
            "paid_rub": value["amount"],
            "payment_id": expected_payment,
        }

    async def execute(self, executor, action):
        value = action["payload"]
        self.guard(action)
        if self.store.setting(intent_key(action["id"]), {}).get("started_at"):
            raise APIError("PREMIUM_PAYMENT_ALREADY_STARTED", uncertain=True)
        fresh = await self.preview(value["actor"], action["target"], value["quote_id"])
        if any(
            fresh.get(k) != value.get(k)
            for k in (
                "account_id",
                "item",
                "quote_id",
                "amount",
                "period",
                "policy_revision",
                "before_payment_id",
            )
        ):
            raise ValidationError("Данные платного размещения изменились")
        latest = await executor.owned_item(action["target"])
        if self.snapshot(latest) != value["item"]:
            raise ValidationError("Товар изменился при подготовке условий")
        await executor.products.prepare_item_agreements(latest)
        fresh = await self.preview(value["actor"], action["target"], value["quote_id"])
        if any(
            fresh.get(k) != value.get(k)
            for k in (
                "account_id",
                "item",
                "quote_id",
                "amount",
                "period",
                "policy_revision",
                "before_payment_id",
            )
        ):
            raise ValidationError("Тариф или товар изменились перед оплатой")
        viewer = await self.api.call("get")
        balance = (viewer.get("profile") or {}).get("balance", {}).get("available")
        with self.store.transaction():
            self.guard(action)
            if not quote_fresh(fresh["checked_at"], max_age=30):
                raise ValidationError("Тариф устарел до начала платежа")
            reserved = sum(
                (
                    money(r["amount"])
                    for r in self.store.rows("SELECT amount FROM promotion_expenses WHERE status='RESERVED'")
                ),
                Decimal(0),
            )
            if viewer.get("id") != value["account_id"] or balance is None or money(balance) < reserved:
                raise ValidationError("Баланс Playerok не покрывает зарезервированные расходы")
            # Move the reservation to the actual dispatch period and recheck caps atomically.
            self.store.execute(
                "UPDATE promotion_expenses SET created_at=? WHERE operation_key=?",
                (now(), action["operation_key"]),
            )
            _, policy, _ = self.settings.get()
            local = datetime.now(ZoneInfo("Europe/Moscow"))
            day = local.replace(hour=0, minute=0, second=0, microsecond=0)
            month = day.replace(day=1)
            totals = {"day": Decimal(0), "month": Decimal(0), "count": 0}
            for row in self.store.rows(
                "SELECT * FROM promotion_expenses WHERE status IN ('RESERVED','SUCCEEDED','UNCERTAIN')"
            ):
                at = datetime.fromisoformat(row["created_at"])
                if at >= day:
                    totals["day"] += money(row["amount"])
                if at >= month:
                    totals["month"] += money(row["amount"])
                if row["item_id"] == action["target"]:
                    totals["count"] += 1
            if "manual_authority" not in value and (
                money(value["amount"]) > money(policy.per_operation)
                or totals["day"] > money(policy.daily)
                or totals["month"] > money(policy.monthly)
                or totals["count"] > policy.max_operations_per_item
            ):
                raise ValidationError("Лимит расходов изменился к моменту отправки")
            self.store.set_setting(
                intent_key(action["id"]),
                {
                    "operation_id": action["id"],
                    "started_at": now(),
                    "checked_at": fresh["checked_at"],
                    "pending": True,
                },
            )
        try:
            result = await executor.mutate(
                action,
                "publishItem" if action["kind"] == "premium_publish" else "increaseItemPriorityStatus",
                "publish_item" if action["kind"] == "premium_publish" else "increase_item_priority_status",
                item_id=action["target"],
                priority_status_id=value["quote_id"],
            )
            candidate = (result.get("status_payment") or {}).get("id") if isinstance(result, dict) else None
            intent = self.store.setting(intent_key(action["id"]))
            self.store.set_setting(intent_key(action["id"]), {**intent, "candidate_id": candidate})
            current = await executor.owned_item(action["target"])
            receipt = self.receipt({**value, "operation_id": action["id"]}, current, candidate)
            self.store.set_setting(
                intent_key(action["id"]),
                {**intent, "candidate_id": candidate, "receipt_id": candidate, "pending": False},
            )
            return receipt
        except Exception:  # noqa: BLE001 -- any failure after intent preserves payment uncertainty
            raise APIError("PREMIUM_PAYMENT_RESULT_UNKNOWN", uncertain=True) from None

    async def reconcile(self, action):
        value = json.loads(action["payload"]) if isinstance(action["payload"], str) else action["payload"]
        if self.api.account.id != value["account_id"]:
            raise Conflict("Аккаунт сверки не совпадает")
        candidate = self.store.setting(intent_key(action["id"]), {}).get("candidate_id")
        if not candidate:
            raise Conflict("Ответ платежа потерян. Нет однозначного ID квитанции; повтор запрещён")
        item = await self.settings.access.item(value["actor"], action["target"])
        return self.receipt({**value, "operation_id": action["id"]}, item, candidate)
