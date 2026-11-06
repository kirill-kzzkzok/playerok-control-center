"""Paid placement through existing Outbox, budgets and exact server payment receipts."""

import asyncio
import json
import re
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.database.store import Conflict, now
from app.playerok.adapter import APIError, validate_page
from app.services.finance import money, quote_fresh
from app.services.premium_review import blocking_expenses, debit_reviewed_elsewhere, release_ids
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


def placement_funds(viewer):
    """Official priority modal: available OR pendingIncome, never their sum.

    LOCAL is sent once; the placement API selects the internal source. `frozen`
    is not pendingIncome, and administrative hasFrozenBalance remains a veto.
    All outstanding placement reservations are conservatively charged to either pool.
    """
    profile = viewer.get("profile") or {}
    if viewer.get("has_frozen_balance") is True or profile.get("has_frozen_balance") is True:
        raise ValidationError("Playerok заблокировал операции с балансом. Это не ожидаемый доход от продаж")
    balance = profile.get("balance") or {}
    if balance.get("available") is None:
        raise ValidationError("Доступный баланс Playerok не подтверждён")
    available = money(balance["available"])
    pending = money(balance.get("pending_income") or 0)
    return available, pending, max(available, pending)


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

    def check_reviewed_manual_budget(self, value, policy, *, already_reserved=False):
        if not self.store.rows("SELECT 1 FROM settings WHERE key LIKE 'promotions:review:%' LIMIT 1"):
            return
        # Zero automatic limits mean there is no standing authorization. A new
        # exact-price manual approval still supplies its own per-purchase cap.
        active_caps = {
            name
            for name, limit in (
                ("лимит на одну операцию", policy.per_operation),
                ("дневной лимит", policy.daily),
                ("месячный лимит", policy.monthly),
                ("лимит операций на лот", policy.max_operations_per_item),
            )
            if money(limit) > 0
        }
        quote = {"price": "0" if already_reserved else value["amount"], "checked_at": now()}
        if already_reserved:
            policy = replace(policy, max_operations_per_item=policy.max_operations_per_item + 1)
        reasons = set(dry_run(self.store, value["item"]["id"], quote, policy, datetime.now(UTC))["reasons"])
        if money(policy.per_operation) and money(value["amount"]) > money(policy.per_operation):
            reasons.add("лимит на одну операцию")
        if active_caps & reasons:
            raise ValidationError("Расход не разрешён: " + ", ".join(sorted(active_caps & reasons)))

    @staticmethod
    def snapshot(item):
        return {k: item.get(k) for k in ("id", "status", "price", "raw_price", "priority")}

    async def preview(self, owner, ident, quote_id, *, native_relist=False):
        item = await self.settings.access.item(owner, ident)
        self.permissions()
        if native_relist:
            from app.services.logical_products import LogicalProducts

            logical = LogicalProducts(self.store, self.api)
            logical.authority(owner)
            binding = logical.listing(ident)
            if binding:
                logical.verify_listing(binding, item)
        if native_relist and (item.get("status") != "SOLD" or item.get("may_be_published") is not True):
            raise ValidationError("Playerok не разрешил штатное повторное размещение этого товара")
        if item.get("status") not in ({"SOLD"} if native_relist else {"DRAFT", "APPROVED"}):
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
            **({"native_relist": True} if native_relist else {}),
        }

    def authority(self, value):
        revision, policy, selection = self.settings.get()
        approval = self.store.setting("promotions:approved:" + value["item"]["id"], {})
        if not policy.enabled:
            raise ValidationError("Автоподнятие остановлено владельцем")
        if revision != value["policy_revision"]:
            raise ValidationError(
                "Эта заявка создана до изменения настроек продвижения и больше не действует"
            )
        if not self.has_authority(value["item"]["id"], revision):
            raise ValidationError("Расходы по текущим лимитам ещё не разрешены владельцем")
        if not self.settings.selected(value["item"]["id"], selection):
            raise ValidationError("Товар исключён из автоподнятия")
        if not approval:
            raise ValidationError("Для этого товара ещё не подтверждён тариф автоподнятия")
        # Budget consent is revision-bound above. The tariff approval records
        # exact monetary terms; its historical policy revision is not a term.
        # Never rewrite it or carry budget authority across a financial edit.
        if approval.get("account_id") != value.get("account_id"):
            raise ValidationError("Подтверждение тарифа относится к другому аккаунту Playerok")
        if any(approval.get(k) != value.get(k) for k in ("quote_id", "amount", "type", "period")):
            raise ValidationError(
                "Playerok изменил стоимость, срок или вид подтверждённого тарифа. Нужно новое согласие на этот тариф"
            )
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
        fresh = await self.preview(
            owner, value["item"]["id"], value["quote_id"], native_relist=value.get("native_relist", False)
        )
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
        if value.get("native_relist") and (not manual or automatic or launch):
            raise ValidationError("Повторное PREMIUM-размещение требует отдельного разового согласия")
        if manual and (automatic or launch):
            raise ValidationError("Разовое разрешение не включает расписание или составной запуск")
        existing = self.store.rows("SELECT id FROM outbox WHERE operation_key=?", (key,))
        if existing:
            return existing[0]["id"]
        fresh = await self.preview(
            owner, value["item"]["id"], value["quote_id"], native_relist=value.get("native_relist", False)
        )
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
        if fresh["item"]["status"] == "DRAFT" and not launch:
            raise ValidationError("Для черновика сначала настройте выдачу и подтвердите единый запуск товара")
        self.permissions(fresh["account_id"], automatic)
        viewer = await self.api.call("get")
        if viewer.get("id") != fresh["account_id"]:
            raise ValidationError("Аккаунт баланса не соответствует товару")
        _, _, available = placement_funds(viewer)
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
            if blocking_expenses(self.store):
                raise Conflict("Предыдущий расход Playerok неизвестен. Сначала выполните сверку")
            if manual:
                self.check_reviewed_manual_budget(fresh, policy)
            reserved = sum(
                (
                    money(r["amount"])
                    for r in self.store.rows("SELECT amount FROM promotion_expenses WHERE status='RESERVED'")
                ),
                Decimal(0),
            )
            if money(available) < reserved + money(fresh["amount"]):
                raise ValidationError(
                    "Недостаточно баланса Playerok или средств в заморозке с учётом резервов; источники не складываются"
                )
            kind = "premium_publish" if fresh["item"]["status"] in {"DRAFT", "SOLD"} else "premium_bump"
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
        if value.get("native_relist") and (
            action["kind"] != "premium_publish"
            or not value.get("manual_authority")
            or value.get("item", {}).get("status") != "SOLD"
        ):
            raise ValidationError("Нет точного разрешения повторной публикации")
        self.permissions(value["account_id"], value["automatic"])
        self.settings.access.orders.owner(value["actor"])
        revision, _, _ = self.settings.get()
        if revision != value["policy_revision"]:
            raise ValidationError("Настройки расходов изменились; платёж остановлен")
        if "manual_authority" in value and (
            value["automatic"]
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
        if blocking_expenses(self.store):
            raise ValidationError("Есть расход с неизвестным результатом")
        released = {intent_key(ident) for ident in release_ids(self.store)}
        for row in self.store.rows("SELECT key,value FROM settings WHERE key LIKE 'promotions:intent:%'"):
            if row["key"] not in released | {intent_key(action["id"])} and json.loads(row["value"]).get(
                "pending"
            ):
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
            or payment.get("provider_id") not in {"LOCAL", "PENDING_INCOME"}
            or payment.get("value") is None
            or money(payment["value"]) != money(value["amount"])
        ):
            raise APIError("PREMIUM_PAYMENT_NOT_CONFIRMED", uncertain=True)
        self.unused_receipt(expected_payment, value.get("operation_id"))
        return {
            "remote_id": item["id"],
            "name": item.get("name"),
            "status": item["status"],
            "priority": "PREMIUM",
            "paid_rub": value["amount"],
            "payment_id": expected_payment,
        }

    def unused_receipt(self, payment, operation):
        if debit_reviewed_elsewhere(self.store, payment, operation) or self.store.rows(
            "SELECT 1 FROM outbox WHERE id<>? AND json_extract(result,'$.payment_id')=? LIMIT 1",
            (operation, payment),
        ):
            raise APIError("PREMIUM_RECEIPT_ALREADY_USED", uncertain=True)
        for row in self.store.rows("SELECT value FROM settings WHERE key LIKE 'promotions:intent:%'"):
            prior = json.loads(row["value"])
            if prior.get("receipt_id") == payment and prior.get("operation_id") != operation:
                raise APIError("PREMIUM_RECEIPT_ALREADY_USED", uncertain=True)

    def update_intent(self, ident, **updates):
        try:
            with self.store.transaction():
                intent = self.store.setting(intent_key(ident), {})
                self.store.set_setting(intent_key(ident), {**intent, **updates})
        except Exception as error:
            raise APIError("PREMIUM_RECEIPT_PERSISTENCE_FAILED", uncertain=True) from error

    def diagnostic(self, ident, stage, *, error=None, candidate=None, checks=None):
        reason = getattr(error, "code", type(error).__name__) if error else "CHECKING"
        if not isinstance(reason, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", reason):
            reason = "UNCLASSIFIED_ERROR"
        self.update_intent(
            ident,
            diagnostic={
                "stage": stage,
                "reason": reason,
                "candidate_present": bool(candidate),
                "checks": checks or {},
            },
        )

    async def confirm_receipt(self, action, *, persist=False):
        """PREPARE with reads only; caller decides whether to journal discovery.

        A mutation candidate is causal evidence. Discovery from a subsequent
        item requires an acknowledged mutation, the item's exact receipt link
        and an independent, narrowly timed transaction, never amount-only search.
        """
        value = json.loads(action["payload"]) if isinstance(action["payload"], str) else action["payload"]
        intent = self.store.setting(intent_key(action["id"]), {})
        candidate = intent.get("candidate_id")
        if intent.get("operation_id") != action["id"]:
            raise Conflict("Журнал платежа не соответствует операции")
        acknowledged = intent.get("mutation_returned") or "candidate_id" in intent
        if not acknowledged:
            raise Conflict("Ответ платежа потерян. Нет однозначного ID квитанции; повтор запрещён")
        confirmed = intent.get("confirmed_receipt")
        if isinstance(confirmed, dict) and (
            confirmed.get("remote_id") == action["target"] == value["item"]["id"]
            and confirmed.get("payment_id") == intent.get("receipt_id") == candidate
            and candidate != value.get("before_payment_id")
            and money(confirmed.get("paid_rub")) == money(value["amount"])
            and self.api.account.id == value["account_id"]
            and self.store.setting("playerok:account_id", value["account_id"]) == value["account_id"]
            and intent.get("diagnostic", {}).get("reason") == "CONFIRMED"
        ):
            self.settings.access.orders.owner(value["actor"])
            self.unused_receipt(candidate, action["id"])
            return confirmed  # Durable successful validation is not erased by a later outage.
        discovery = intent.get("candidate_source") == "ITEM_READBACK"
        anchored = discovery and intent.get("candidate_target") == action["target"]
        last_error = APIError("PREMIUM_PAYMENT_NOT_CONFIRMED", uncertain=True)
        stage, checks = "READBACK", {}
        for attempt in range(3):
            if attempt:
                await asyncio.sleep((0.5, 1.5)[attempt - 1])
            try:
                stage = "READBACK"
                if persist:
                    self.diagnostic(action["id"], stage, candidate=candidate)
                # Receipt PREPARE must not refresh titles/projections or write
                # any local product data (including during owner diagnostics).
                self.settings.access.orders.owner(value["actor"])
                item = await self.api.call("get_item", id=action["target"])
                checks = {
                    "item": item.get("id") == action["target"] == value["item"]["id"],
                    "owner": (item.get("user") or {}).get("id") == value["account_id"] == self.api.account.id,
                }
                stage = "RECEIPT_VALIDATION"
                if not all(checks.values()):
                    raise APIError("PREMIUM_RECEIPT_ITEM_MISMATCH", uncertain=True)
                payment = item.get("status_payment") or {}
                observed = payment.get("id")
                if (
                    (not candidate or candidate == value.get("before_payment_id"))
                    and acknowledged
                    and observed
                    and observed != value.get("before_payment_id")
                ):
                    candidate, discovery, anchored = observed, True, True
                    if persist:
                        # Save the exact item-linked ID BEFORE the history RPC.
                        self.update_intent(
                            action["id"],
                            candidate_id=candidate,
                            candidate_source="ITEM_READBACK",
                            candidate_target=action["target"],
                        )
                checks.update(
                    id=bool(candidate) and observed == candidate,
                    new_id=bool(candidate) and candidate != value.get("before_payment_id"),
                    amount=payment.get("value") is not None
                    and money(payment["value"]) == money(value["amount"]),
                    direction=payment.get("direction") == "OUT",
                    operation=payment.get("operation") == "ITEM_PREMIUM_PRIORITY",
                    status=payment.get("status") == "CONFIRMED",
                    provider=payment.get("provider_id") in {"LOCAL", "PENDING_INCOME"},
                )
                if persist:
                    self.diagnostic(action["id"], stage, candidate=candidate, checks=checks)
                if not candidate or candidate == value.get("before_payment_id"):
                    raise APIError("PREMIUM_NEW_RECEIPT_NOT_VISIBLE", uncertain=True)
                if discovery and not anchored:
                    raise Conflict("Нет сохранённой серверной связи квитанции с этим товаром")
                if not discovery and observed == candidate and item.get("status") != "SOLD":
                    result = self.receipt({**value, "operation_id": action["id"]}, item, candidate)
                else:
                    # A different later item receipt never replaces a valid
                    # mutation candidate. Find the EXACT ID in account history.
                    stage = "READBACK"
                    result = await self.historical_receipt(
                        action, value, item, candidate, max_age=30 if discovery else 600
                    )
                    stage = "RECEIPT_VALIDATION"
                    if discovery and observed == candidate and item.get("status") != "SOLD":
                        self.receipt({**value, "operation_id": action["id"]}, item, candidate)
                    checks.update(result.get("receipt_checks", {}))
                    checks["item_anchor"] = bool(anchored or not discovery)
                self.unused_receipt(candidate, action["id"])
                if persist:
                    with self.store.transaction() as db:
                        current = db.execute(
                            "SELECT status,lease_token,lease_until FROM outbox WHERE id=?", (action["id"],)
                        ).fetchone()
                        if (
                            not current
                            or current["status"] != "PROCESSING"
                            or current["lease_token"] != action["lease_token"]
                            or (current["lease_until"] or 0) <= time.time()
                        ):
                            raise APIError("PREMIUM_CONFIRMATION_LEASE_LOST", uncertain=True)
                        self.unused_receipt(candidate, action["id"])
                        self.update_intent(
                            action["id"],
                            receipt_id=candidate,
                            pending=False,
                            confirmed_receipt=result,
                            diagnostic={
                                "stage": "RECEIPT_VALIDATION",
                                "reason": "CONFIRMED",
                                "candidate_present": True,
                                "checks": checks,
                            },
                        )
                return result
            except (APIError, Conflict, ValueError, KeyError, TypeError) as error:
                last_error = error
                if isinstance(error, APIError) and error.diagnostics:
                    checks.update(error.diagnostics.get("receipt_checks", {}))
                    stage = "RECEIPT_VALIDATION"
                if isinstance(error, APIError) and error.code == "PREMIUM_RECEIPT_PERSISTENCE_FAILED":
                    raise
                if persist:
                    self.diagnostic(action["id"], stage, error=error, candidate=candidate, checks=checks)
        raise last_error

    async def execute(self, executor, action):
        value = action["payload"]
        self.guard(action)
        if self.store.setting(intent_key(action["id"]), {}).get("started_at"):
            raise APIError("PREMIUM_PAYMENT_ALREADY_STARTED", uncertain=True)
        fresh = await self.preview(
            value["actor"],
            action["target"],
            value["quote_id"],
            native_relist=value.get("native_relist", False),
        )
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
        fresh = await self.preview(
            value["actor"],
            action["target"],
            value["quote_id"],
            native_relist=value.get("native_relist", False),
        )
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
        _, _, balance = placement_funds(viewer)
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
            if "manual_authority" in value:
                self.check_reviewed_manual_budget(value, policy, already_reserved=True)
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
                    "automatic_receipt_check": True,
                },
            )
        stage = "MUTATION_RESPONSE"
        try:
            self.diagnostic(action["id"], "MUTATION_RESPONSE")
            result = await executor.mutate(
                action,
                "publishItem" if action["kind"] == "premium_publish" else "increaseItemPriorityStatus",
                "publish_item" if action["kind"] == "premium_publish" else "increase_item_priority_status",
                item_id=action["target"],
                priority_status_id=value["quote_id"],
            )
            stage = "CANDIDATE_EXTRACTION"
            candidate = (result.get("status_payment") or {}).get("id") if isinstance(result, dict) else None
            self.update_intent(
                action["id"],
                candidate_id=candidate,
                candidate_source="MUTATION_RESPONSE",
                mutation_returned=True,
                diagnostic={
                    "stage": stage,
                    "reason": "CHECKING",
                    "candidate_present": bool(candidate),
                    "checks": {},
                },
            )
            stage = "READBACK"
            return await self.confirm_receipt(action, persist=True)
        except Exception as error:  # noqa: BLE001 -- never repeat a possibly accepted POST
            diagnostic = self.store.setting(intent_key(action["id"]), {}).get("diagnostic", {})
            stage = (
                "PERSISTENCE"
                if isinstance(error, APIError) and error.code == "PREMIUM_RECEIPT_PERSISTENCE_FAILED"
                else diagnostic.get("stage", stage)
                if stage == "READBACK"
                else stage
            )
            try:
                self.diagnostic(
                    action["id"],
                    stage,
                    error=error,
                    candidate=self.store.setting(intent_key(action["id"]), {}).get("candidate_id"),
                    checks=diagnostic.get("checks"),
                )
            except APIError:
                from app.security.logging import event

                event("CHECK", "PREMIUM receipt diagnostic: PERSISTENCE", level=10)
            raise APIError("PREMIUM_PAYMENT_RESULT_UNKNOWN", uncertain=True) from None

    async def reconcile(self, action):
        value = json.loads(action["payload"]) if isinstance(action["payload"], str) else action["payload"]
        if self.api.account.id != value["account_id"]:
            raise Conflict("Аккаунт сверки не совпадает")
        return await self.confirm_receipt(action)

    async def historical_receipt(self, action, value, item, candidate, *, max_age=600):
        """A sold item no longer exposes the receipt of each earlier placement."""
        from playerokapi.enums import TransactionOperations

        intent = self.store.setting(intent_key(action["id"]), {})
        try:
            started = datetime.fromisoformat(intent["started_at"])
            if started.tzinfo is None or candidate == value.get("before_payment_id"):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise Conflict("Не подтверждены квитанция и время начала платежа") from None
        cursor, seen = None, []
        for _ in range(4):
            page = await self.api.call(
                "get_transactions",
                operation=TransactionOperations.ITEM_PREMIUM_PRIORITY,
                from_date=started.astimezone(UTC) - timedelta(seconds=1),
                to_date=started.astimezone(UTC) + timedelta(seconds=max_age + 1),
                count=24,
                after_cursor=cursor,
            )
            transactions, following = validate_page(page, "transactions", seen)
            matches = [t for t in transactions if t.get("id") == candidate]
            if matches:
                payment = matches[0]
                checks = {}
                try:
                    created = datetime.fromisoformat(payment["created_at"])
                    checks = {
                        "history_id": len(matches) == 1 and payment.get("id") == candidate,
                        "history_status": payment.get("status") == "CONFIRMED",
                        "history_direction": payment.get("direction") == "OUT",
                        "history_operation": payment.get("operation") == "ITEM_PREMIUM_PRIORITY",
                        "history_provider": payment.get("provider_id") in {"LOCAL", "PENDING_INCOME"},
                        "history_account": (payment.get("user") or {}).get("id")
                        == value["account_id"]
                        == self.api.account.id
                        == self.store.setting("playerok:account_id"),
                        "history_amount": money(payment.get("value")) == money(value["amount"]),
                        "history_time": created.tzinfo is not None
                        and started <= created <= started + timedelta(seconds=max_age),
                    }
                    valid = all(checks.values())
                except (KeyError, TypeError, ValueError):
                    valid = False
                if not valid:
                    raise APIError(
                        "PREMIUM_HISTORICAL_RECEIPT_NOT_CONFIRMED",
                        uncertain=True,
                        diagnostics={"receipt_checks": checks},
                    )
                if debit_reviewed_elsewhere(self.store, candidate, action["id"]):
                    raise Conflict("Списание уже учтено в другой операции")
                if self.store.rows(
                    "SELECT 1 FROM outbox WHERE id<>? AND json_extract(result,'$.payment_id')=? LIMIT 1",
                    (action["id"], candidate),
                ):
                    raise Conflict("Квитанция уже использована другой операцией")
                for row in self.store.rows("SELECT value FROM settings WHERE key LIKE 'promotions:intent:%'"):
                    prior = json.loads(row["value"])
                    if prior.get("receipt_id") == candidate and prior.get("operation_id") != action["id"]:
                        raise Conflict("Квитанция уже использована другой операцией")
                return {
                    "remote_id": item["id"],
                    "name": item.get("name"),
                    "status": item["status"],
                    "priority": item.get("priority"),
                    "paid_rub": str(money(payment["value"])),
                    "payment_id": candidate,
                    "historical_payment": True,
                    "receipt_checks": checks,
                    "payment_created_at": payment["created_at"],
                }
            if not following:
                break
            cursor = following
            seen.append(cursor)
        raise Conflict("Историческая квитанция не найдена. Исход неизвестен; повтор платежа запрещён")

    async def reconcile_due(self):
        """Only new acknowledged intents opt into read-only background proof.

        Legacy operations and explicit owner decisions require their existing
        reconciliation route; loading a new version must not rewrite them.
        """
        rows = self.store.rows(
            "SELECT o.* FROM outbox o JOIN settings s ON s.key='promotions:intent:'||o.id "
            "WHERE o.kind IN ('premium_publish','premium_bump') AND o.status='UNCERTAIN' "
            "AND json_extract(s.value,'$.automatic_receipt_check')=1 "
            "AND json_type(s.value,'$.candidate_id') IS NOT NULL "
            "AND coalesce(json_extract(s.value,'$.receipt_retry_at'),0)<=? "
            "AND NOT EXISTS(SELECT 1 FROM operation_resolutions r WHERE r.operation_id=o.id) "
            "AND NOT EXISTS(SELECT 1 FROM settings r WHERE r.key='promotions:review:'||o.id) "
            "ORDER BY o.created_at LIMIT 1",
            (time.time(),),
        )
        for action in rows:
            from app.services.reconciliation import Reconciliation, signature

            value = json.loads(action["payload"])
            try:
                await Reconciliation(self.store, self.api).resolve(
                    value["actor"],
                    action["id"],
                    signature(action),
                    "CONFIRM_READ_STATE",
                    "Автоматическая проверка точной серверной квитанции",
                    automatic=True,
                )
            except (APIError, Conflict, ValueError, KeyError, TypeError) as error:
                intent = self.store.setting(intent_key(action["id"]), {})
                failures = intent.get("receipt_failures", 0) + 1
                self.update_intent(
                    action["id"],
                    receipt_failures=failures,
                    receipt_retry_at=time.time() + min(300, 30 * 2 ** min(failures, 4)),
                )
                self.diagnostic(action["id"], "READBACK", error=error, candidate=intent.get("candidate_id"))
