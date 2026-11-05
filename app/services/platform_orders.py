"""Playerok delivers the goods; reuse durable chains for seller follow-up only."""

import json
from dataclasses import asdict, replace

from app.database.store import Conflict, encode, now, server_time
from app.services.automation import Automation, DeliveryPolicy, future_order, message_text
from app.services.logical_products import LogicalProducts
from app.services.order_management import deal_details, sale
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

    async def save(
        self, owner, item_id, revision, *, reply, fulfill, text, switch_confirmed=False, expected_text=None
    ):
        account = self.logical.authority(owner)
        item = await self.policies.item(owner, item_id)
        if item.get("deleted_at") or item.get("status") not in PLATFORM_STATUSES:
            raise ValidationError("Состояние товара не разрешает настройку. Откройте его на Playerok")
        policy = DeliveryPolicy(
            mode="playerok",
            enabled=reply or fulfill,
            paid_reply=reply,
            mark_sent=fulfill,
            auto_enroll=reply or fulfill,
            greeting=message_text(text, required=reply),
            account_id=account,
        )
        current, old_policy = self.policies.get(item_id)
        if current != revision:
            raise Conflict("Настройки изменились. Откройте товар заново")
        product = self.logical.listing(item_id)
        if (
            product
            and json.loads(product["config"]).get("platform_only")
            and product["enabled"]
            and old_policy.enabled
            and old_policy.mode == "playerok"
            and old_policy.mark_sent != policy.mark_sent
            and {
                k: v
                for k, v in asdict(old_policy).items()
                if k not in {"mark_sent", "enabled", "auto_enroll"}
            }
            == {k: v for k, v in asdict(policy).items() if k not in {"mark_sent", "enabled", "auto_enroll"}}
        ):
            self.logical.verify_listing(product, item)
            self.logical.update_after(owner, item_id, product, policy.mark_sent, field="completion")
            return {"saved": True}
        if (
            product
            and json.loads(product["config"]).get("platform_only")
            and old_policy.mode == "playerok"
            and {k: v for k, v in asdict(old_policy).items() if k != "greeting"}
            == {k: v for k, v in asdict(policy).items() if k != "greeting"}
        ):
            self.logical.verify_listing(product, item)
            with self.store.transaction():
                latest_revision, latest = self.policies.get(item_id)
                if (
                    latest_revision != revision
                    or latest != old_policy
                    or expected_text is not None
                    and old_policy.greeting != expected_text
                ):
                    raise Conflict("Сообщение изменилось во время ввода. Откройте настройки заново")
                self.logical.update_after(owner, item_id, product, policy.greeting, field="greeting")
            return {"saved": True}
        gift = self.store.rows("SELECT enabled FROM gift_listings WHERE item_id=?", (item_id,))
        replacing = bool(gift and gift[0]["enabled"]) or bool(
            product
            and product["handler"] == "telegram_gifts"
            and not json.loads(product["config"]).get("platform_only")
        )
        if replacing and not switch_confirmed:
            return {"switch": True}
        if not product or product["state"] != "VERIFIED":
            if product and product["state"] == "PENDING":
                rows = self.store.rows(
                    "SELECT status,started_at,error_code FROM outbox WHERE id=?", (product["operation_id"],)
                )
                if (
                    not rows
                    or rows[0]["status"] != "PERMANENT_FAILED"
                    or rows[0]["started_at"] is not None
                    or rows[0]["error_code"] == "MANUALLY_STOPPED_UNCERTAIN"
                ):
                    return {"operation": product["operation_id"]}
            prepared = await self.logical.prepare(
                owner, item_id, "manual", {"platform_only": True}, platform_binding=True
            )
            return {
                "binding": prepared,
                "settings": {
                    "owner": owner,
                    "item_id": item_id,
                    "revision": revision,
                    "policy": asdict(policy),
                    "switch_confirmed": switch_confirmed,
                },
            }
        self.logical.verify_listing(product, item)
        with self.store.transaction() as db:
            commit_platform_settings(db, owner, item_id, revision, policy, switch_confirmed=switch_confirmed)
        return {"saved": True}

    def confirm(self, owner, request):
        settings = request["settings"]
        if settings["owner"] != owner:
            raise Conflict("Подтверждение принадлежит другому владельцу")
        with self.store.transaction() as db:
            revision, _ = self.policies.get(settings["item_id"])
            if revision != settings["revision"]:
                raise Conflict("Настройки изменились до подтверждения")
            result = self.logical.commit(owner, request["binding"])
            row = db.execute("SELECT payload FROM outbox WHERE id=?", (result["operation_id"],)).fetchone()
            payload = json.loads(row[0])
            if "platform_settings" not in payload:
                product = self.logical.product(result["product_id"], owner)
                payload["platform_settings"] = {
                    **settings,
                    "control_revision": product["control_revision"],
                    "stop_token": stop_token(db),
                }
                db.execute(
                    "UPDATE outbox SET payload=? WHERE id=?", (encode(payload), result["operation_id"])
                )
            return result

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
        local = self.store.rows("SELECT item_id FROM orders WHERE id=?", (ident,))
        if not local:
            raise Conflict("Оплаченный заказ ещё не сохранён")
        item = local[0]["item_id"]
        policy = self.authority(item, revision)
        order = await deal_details(self.api, ident, need_chat=policy.paid_reply, need_created=True)
        platform_sale(order, ident, policy.account_id, item=item)
        self.policies.orders.orders.ingest(order)
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
            had_scope = db.execute(
                "SELECT config FROM logical_order_scopes WHERE order_id=?", (ident,)
            ).fetchone()
            self.logical.pin_order(db, ident, item, "playerok")
            if had_scope:
                saved = json.loads(had_scope["config"])
                policy = replace(
                    policy,
                    greeting=saved.get("greeting", policy.greeting),
                    mark_sent=saved.get("mark_sent", policy.mark_sent),
                )
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
        order = await deal_details(self.api, action["target"], need_chat=action["kind"] == "chain_message")
        platform_sale(order, action["target"], policy.account_id, item=chain["item_id"])
        self.policies.orders.orders.ingest(order)
        self.guard(action)
        return order


PLATFORM_STATUSES = {
    "DRAFT",
    "APPROVED",
    "SOLD",
    "EXPIRED",
    "DISCONTINUED",
    "DECLINED",
    "PENDING_APPROVAL",
    "PENDING_MODERATION",
}


def stop_token(db):
    return db.execute(
        "SELECT COALESCE(MAX(rowid),0) FROM audit_log WHERE action='STOP_ALL_DELIVERY_POLICIES'"
    ).fetchone()[0]


def commit_platform_settings(
    db, owner, item_id, revision, policy, *, switch_confirmed=False, prepublication_cutoff=None, inherit=None
):
    """One local commit, only after independent VERIFIED description evidence."""
    account = db.execute("SELECT value FROM settings WHERE key='playerok:account_id'").fetchone()
    if (
        not account
        or json.loads(account[0]) != policy.account_id
        or not db.execute("SELECT 1 FROM administrators WHERE user_id=?", (owner,)).fetchone()
    ):
        raise Conflict("Аккаунт или владелец изменился")
    row = db.execute("SELECT revision FROM delivery_policies WHERE item_id=?", (item_id,)).fetchone()
    if (row[0] if row else 0) != revision:
        raise Conflict("Настройки изменились во время подтверждения")
    product = db.execute(
        "SELECT p.*,l.config_revision,l.verified_at FROM logical_products p JOIN logical_listings l ON l.product_id=p.id WHERE l.item_id=?",
        (item_id,),
    ).fetchone()
    if (
        not product
        or product["state"] != "VERIFIED"
        or product["revision"] != product["config_revision"]
        or not product["verified_at"]
        or product["error_code"] is not None
        or product["owner_id"] != owner
        or product["account_id"] != policy.account_id
    ):
        raise Conflict("Сначала подтвердите постоянную привязку товара")
    ids = [
        r[0] for r in db.execute("SELECT item_id FROM logical_listings WHERE product_id=?", (product["id"],))
    ]
    placeholders = ",".join("?" for _ in ids)
    if not switch_confirmed and (
        product["handler"] == "telegram_gifts"
        and not json.loads(product["config"]).get("platform_only")
        or db.execute(
            f"SELECT 1 FROM gift_listings WHERE item_id IN ({placeholders}) AND enabled=1", ids
        ).fetchone()
    ):
        raise Conflict("Нужно явно подтвердить замену Telegram Gifts")
    if policy.enabled and (
        db.execute(
            f"SELECT 1 FROM orders o JOIN outbox b ON b.target=o.id WHERE o.item_id IN ({placeholders}) AND b.kind IN ('delivery','delivery_text','gift_send','chain_message','fulfill') AND (b.status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') OR b.error_code='MANUALLY_STOPPED_UNCERTAIN') LIMIT 1",
            ids,
        ).fetchone()
        or db.execute(
            f"SELECT 1 FROM delivery_chains WHERE item_id IN ({placeholders}) AND status IN ('ACTIVE','BLOCKED') LIMIT 1",
            ids,
        ).fetchone()
    ):
        raise Conflict("Есть незавершённая выдача или неизвестный результат; сначала разрешите заказ")
    cutoff = server_time(prepublication_cutoff) if prepublication_cutoff is not None else now()
    if not cutoff:
        raise Conflict("Не подтверждено начало приёма будущих заказов")
    config = {**json.loads(product["config"]), "platform_only": True, "mark_sent": policy.mark_sent}
    db.execute(
        "UPDATE logical_products SET config=?,revision=revision+1,control_revision=control_revision+1,enabled=?,inherit_enabled=?,armed_at=?,updated_at=? WHERE id=?",
        (
            encode(config),
            int(policy.enabled),
            int(policy.enabled and (inherit if inherit is not None else True)),
            cutoff,
            cutoff,
            product["id"],
        ),
    )
    db.execute(
        "UPDATE logical_listings SET config_revision=config_revision+1 WHERE product_id=?", (product["id"],)
    )
    for ident in ids:
        old = db.execute("SELECT revision FROM delivery_policies WHERE item_id=?", (ident,)).fetchone()
        next_revision = (old[0] if old else 0) + 1
        db.execute(
            "UPDATE gift_listings SET enabled=0,revision=revision+1 WHERE item_id=? AND enabled=1", (ident,)
        )
        db.execute(
            "INSERT INTO delivery_policies VALUES(?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET revision=excluded.revision,actor=excluded.actor,payload=excluded.payload,updated_at=excluded.updated_at",
            (ident, next_revision, owner, encode(asdict(policy)), cutoff),
        )
        db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"delivery:{ident}:enabled", encode(policy.enabled)),
        )
        db.execute(
            "INSERT OR IGNORE INTO auto_enrollment(order_id,policy_revision,status,error_code,updated_at) SELECT id,?,'SKIPPED','OWNER_SAVED_AFTER_DISCOVERY',? FROM orders WHERE item_id=?",
            (next_revision, cutoff, ident),
        )
    db.execute(
        "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'SAVE_PLAYEROK_FOLLOWUP',?,'FUTURE_ORDERS_ONLY',?)",
        (owner, item_id, cutoff),
    )


def finish_platform_binding(db, action):
    payload = json.loads(action["payload"]) if isinstance(action["payload"], str) else action["payload"]
    settings = payload.get("platform_settings")
    if not settings:
        return
    try:
        product = db.execute(
            "SELECT control_revision FROM logical_products WHERE id=?",
            (payload["logical_binding"]["product_id"],),
        ).fetchone()
        if (
            stop_token(db) != settings["stop_token"]
            or not product
            or product[0] != settings["control_revision"]
        ):
            raise Conflict("Остановка во время подтверждения")
        commit_platform_settings(
            db,
            settings["owner"],
            settings["item_id"],
            settings["revision"],
            DeliveryPolicy(**settings["policy"]),
            switch_confirmed=settings["switch_confirmed"],
        )
    except (Conflict, ValidationError):
        db.execute(
            "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'PLAYEROK_BINDING_SETTINGS_BLOCKED',?,'REOPEN_SETTINGS_REQUIRED',?)",
            (settings["owner"], settings["item_id"], now()),
        )
