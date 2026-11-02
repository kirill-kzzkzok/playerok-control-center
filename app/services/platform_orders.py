"""Playerok delivers the goods; reuse durable chains for seller follow-up only."""

import json
from dataclasses import asdict

from app.database.store import Conflict, encode, now
from app.services.automation import Automation, DeliveryPolicy, future_order, message_text
from app.services.logical_products import LogicalProducts
from app.services.order_management import sale
from app.services.products import ValidationError

DEFAULT_REPLY = (
    "Спасибо за покупку! ❤️ Товар выдан автоматически. Приятной игры! Если возникнут вопросы — напишите мне"
)
PAID_STATUSES = {"PAID", "SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"}
REASONS = {
    "PLATFORM_REPORT_REQUIRED": "Playerok требует отчёт продавца. Заполните его в сделке на Playerok.",
    "PLATFORM_SYSTEM_FULFILLMENT": "Это системный товар: выполнение отмечает сама площадка.",
    "PLATFORM_ROLE_UNCONFIRMED": "Роль продавца не подтверждена. Проверьте заказ вручную.",
    "PLATFORM_CHAT_UNAVAILABLE": "Чат покупателя недоступен. Откройте заказ и проверьте возможность ответа.",
}


def platform_sale(order, ident, account, *, item=None):
    sale(order, ident, account)
    if (
        not account
        or ((order.get("item") or {}).get("user") or {}).get("id") != account
        or item
        and order["item"]["id"] != item
        or order.get("status") not in PAID_STATUSES
        or order.get("has_problem") is not False
        or order.get("is_automated") is not True
    ):
        raise ValidationError("Не подтверждена оплаченная продажа с выдачей Playerok без проблемы")
    return order


class PlatformOrders:
    def __init__(self, store, api):
        self.store, self.api = store, api
        self.policies = Automation(store, api)
        self.logical = LogicalProducts(store, api)

    async def save(self, owner, item_id, revision, *, reply, fulfill, text):
        account = self.logical.authority(owner)
        item = await self.policies.item(owner, item_id)
        if item.get("status") != "APPROVED" or item.get("deleted_at"):
            raise ValidationError("Выберите опубликованный товар")
        policy = DeliveryPolicy(
            mode="playerok",
            enabled=reply or fulfill,
            paid_reply=reply,
            mark_sent=fulfill,
            auto_enroll=reply or fulfill,
            greeting=message_text(text, required=reply),
            account_id=account,
        )
        with self.store.transaction() as db:
            self.logical.authority(owner, account)
            current, _ = self.policies.get(item_id)
            if current != revision:
                raise Conflict("Настройки изменились. Откройте товар заново")
            product = self.logical.listing(item_id)
            ids = [item_id]
            if product:
                if product["state"] != "VERIFIED" or product["owner_id"] != owner:
                    raise Conflict("Сначала подтвердите существующую привязку товара")
                self.logical.verify_listing(product, item)
                ids = [
                    r[0]
                    for r in db.execute(
                        "SELECT item_id FROM logical_listings WHERE product_id=?", (product["id"],)
                    )
                ]
            placeholders = ",".join("?" for _ in ids)
            if policy.enabled and (
                db.execute(
                    f"SELECT 1 FROM orders o JOIN outbox b ON b.target=o.id WHERE o.item_id IN ({placeholders}) "
                    "AND b.kind IN ('delivery','delivery_text','gift_send','chain_message','fulfill') "
                    "AND (b.status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') OR b.error_code='MANUALLY_STOPPED_UNCERTAIN') LIMIT 1",
                    ids,
                ).fetchone()
                or db.execute(
                    f"SELECT 1 FROM delivery_chains WHERE item_id IN ({placeholders}) AND status IN ('ACTIVE','BLOCKED') LIMIT 1",
                    ids,
                ).fetchone()
            ):
                raise Conflict("Есть незавершённая выдача или неизвестный результат; сначала разрешите заказ")
            cutoff = now()
            if product:
                config = json.loads(product["config"])
                config["platform_only"] = True
                db.execute(
                    "UPDATE logical_products SET config=?,revision=revision+1,control_revision=control_revision+1,enabled=?,armed_at=?,updated_at=? WHERE id=?",
                    (encode(config), int(policy.enabled), cutoff, cutoff, product["id"]),
                )
                db.execute(
                    "UPDATE logical_listings SET config_revision=config_revision+1 WHERE product_id=?",
                    (product["id"],),
                )
            for ident in ids:
                old, _ = self.policies.get(ident)
                db.execute(
                    "UPDATE gift_listings SET enabled=0,revision=revision+1 WHERE item_id=? AND enabled=1",
                    (ident,),
                )
                db.execute(
                    "INSERT INTO delivery_policies VALUES(?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET revision=excluded.revision,actor=excluded.actor,payload=excluded.payload,updated_at=excluded.updated_at",
                    (ident, old + 1, owner, encode(asdict(policy)), cutoff),
                )
                self.store.set_setting(f"delivery:{ident}:enabled", policy.enabled)
                db.execute(
                    "INSERT OR IGNORE INTO auto_enrollment(order_id,policy_revision,status,error_code,updated_at) SELECT id,?,'SKIPPED','OWNER_SAVED_AFTER_DISCOVERY',? FROM orders WHERE item_id=?",
                    (old + 1, cutoff, ident),
                )
            self.store.audit(owner, "SAVE_PLAYEROK_FOLLOWUP", item_id, "FUTURE_ORDERS_ONLY")

    def authority(self, item_id, revision):
        rows = self.store.rows("SELECT actor FROM delivery_policies WHERE item_id=?", (item_id,))
        current, policy = self.policies.get(item_id)
        if not rows or current != revision or policy.mode != "playerok" or not policy.enabled:
            raise Conflict("Настройки выдачи Playerok изменились или остановлены")
        self.logical.authority(rows[0]["actor"], policy.account_id, writes=True)
        if (
            not getattr(self.api.account, "automation_enabled", False)
            or self.store.setting(f"delivery:{item_id}:enabled") is False
        ):
            raise Conflict("Автоматизация остановлена")
        product = self.logical.listing(item_id)
        if product:
            self.logical.require_delivery(item_id, "playerok")
        return policy

    async def create(self, owner, ident, revision):
        self.policies.orders.owner(owner)
        order = await self.policies.orders.fresh(owner, ident)
        item = order["item"]["id"]
        policy = self.authority(item, revision)
        platform_sale(order, ident, policy.account_id)
        product = self.logical.listing(item)
        if product and product["source_order_id"] and product["source_order_id"] != ident:
            raise Conflict("Купленный снимок товара относится к другому заказу")
        with self.store.transaction() as db:
            self.authority(item, revision)
            row = db.execute("SELECT updated_at FROM delivery_policies WHERE item_id=?", (item,)).fetchone()
            if not future_order(order.get("created_at"), row[0]):
                raise Conflict("Автоматизация разрешена только новым покупкам после сохранения")
            if db.execute("SELECT 1 FROM delivery_chains WHERE order_id=?", (ident,)).fetchone():
                return ident
            if (
                db.execute("SELECT 1 FROM gift_jobs WHERE order_id=?", (ident,)).fetchone()
                or db.execute("SELECT 1 FROM reservations WHERE order_id=?", (ident,)).fetchone()
                or db.execute(
                    "SELECT 1 FROM outbox WHERE target=? AND kind IN ('delivery','delivery_text','gift_send','chain_message','fulfill')",
                    (ident,),
                ).fetchone()
            ):
                raise Conflict("По заказу уже зарегистрирована выдача или отметка")
            if db.execute(
                "SELECT 1 FROM order_annotations WHERE order_id=? AND problem=1", (ident,)
            ).fetchone():
                raise Conflict("Заказ требует решения продавца")
            steps = []
            if policy.paid_reply:
                steps.append(("chain_message", policy.greeting))
            if policy.mark_sent and order["status"] == "PAID":
                steps.append(("fulfill", ""))
            db.execute(
                "INSERT INTO delivery_chains VALUES(?,?,?,?,?,'ACTIVE',?)",
                (ident, item, owner, revision, encode(asdict(policy)), now()),
            )
            db.executemany(
                "INSERT INTO delivery_chain_steps(order_id,position,kind,text) VALUES(?,?,?,?)",
                [(ident, i, kind, text) for i, (kind, text) in enumerate(steps)],
            )
        return ident

    def guard(self, action):
        rows = self.store.rows("SELECT * FROM delivery_chains WHERE order_id=?", (action["target"],))
        if not rows:
            raise Conflict("Сценарий заказа не найден")
        chain = rows[0]
        policy = self.authority(chain["item_id"], chain["policy_revision"])
        product = self.logical.listing(chain["item_id"])
        if product and product["source_order_id"] and product["source_order_id"] != action["target"]:
            raise Conflict("Снимок товара принадлежит другому заказу")
        local = self.store.rows("SELECT payload FROM orders WHERE id=?", (action["target"],))
        if not local:
            raise Conflict("Заказ не найден")
        platform_sale(
            json.loads(local[0]["payload"]), action["target"], policy.account_id, item=chain["item_id"]
        )
        return chain, policy

    async def verify(self, action):
        chain, policy = self.guard(action)
        order = await self.api.call("get_deal", deal_id=action["target"])
        platform_sale(order, action["target"], policy.account_id, item=chain["item_id"])
        self.policies.orders.orders.ingest(order)
        self.guard(action)
        return order
