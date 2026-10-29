import json
from dataclasses import asdict, dataclass

from app.database.store import Conflict, encode, now
from app.services.order_management import OrderManagement, deliverable
from app.services.products import ValidationError


def message_text(value, *, required=False):
    if (
        not isinstance(value, str)
        or len(value.encode("utf-16-le")) // 2 > 3500
        or (required and not value.strip())
    ):
        raise ValidationError("Текст должен быть до 3500 символов; данные выдачи не могут быть пустыми")
    return value.strip()


@dataclass(frozen=True)
class DeliveryPolicy:
    enabled: bool = False
    mode: str = "manual"
    text: str = ""
    greeting: str = ""
    after: str = ""
    mark_sent: bool = False

    def __post_init__(self):
        if (
            type(self.enabled) is not bool
            or type(self.mark_sent) is not bool
            or self.mode not in {"manual", "text", "stock"}
        ):
            raise ValidationError("Некорректная политика выдачи")
        message_text(self.text, required=self.mode == "text")
        message_text(self.greeting)
        message_text(self.after)
        if self.enabled and self.mode == "manual":
            raise ValidationError("Для автоматики выберите постоянный текст или уникальный запас")


class Automation:
    """Persisted desired policies and read-only evaluations. No mutation/executor here."""

    def __init__(self, store, api):
        self.store, self.api = store, api
        self.orders = OrderManagement(store, api)

    async def item(self, actor, ident):
        self.orders.owner(actor)
        item = await self.api.call("get_item", id=ident)
        if item.get("id") != ident or (item.get("user") or {}).get("id") != self.api.account.id:
            raise ValidationError("Товар не принадлежит текущей сессии")
        return item

    def get(self, ident):
        rows = self.store.rows("SELECT revision,payload FROM delivery_policies WHERE item_id=?", (ident,))
        if rows:
            return rows[0]["revision"], DeliveryPolicy(**json.loads(rows[0]["payload"]))
        return 0, DeliveryPolicy()

    async def save(self, actor, ident, expected, payload):
        policy = DeliveryPolicy(**payload)
        await self.item(actor, ident)
        with self.store.transaction() as db:
            row = db.execute("SELECT revision FROM delivery_policies WHERE item_id=?", (ident,)).fetchone()
            revision = row[0] if row else 0
            if revision != expected:
                raise Conflict("Настройки изменились в другом сценарии. Откройте их заново")
            db.execute(
                "INSERT INTO delivery_policies VALUES(?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET revision=excluded.revision,actor=excluded.actor,payload=excluded.payload,updated_at=excluded.updated_at",
                (ident, revision + 1, actor, encode(asdict(policy)), now()),
            )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'SAVE_DELIVERY_POLICY',?,?,?)",
                (actor, ident, "DRY_RUN_ONLY", now()),
            )
        return revision + 1

    def stop_all(self, actor):
        self.orders.owner(actor)
        with self.store.transaction() as db:
            count = 0
            for row in db.execute("SELECT item_id,payload FROM delivery_policies").fetchall():
                payload = json.loads(row["payload"])
                if payload.get("enabled"):
                    payload["enabled"] = False
                    db.execute(
                        "UPDATE delivery_policies SET payload=?,revision=revision+1,actor=?,updated_at=? WHERE item_id=?",
                        (encode(payload), actor, now(), row["item_id"]),
                    )
                    count += 1
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'STOP_ALL_DELIVERY_POLICIES','*',?,?)",
                (actor, str(count), now()),
            )
        return count

    async def simulate(self, actor, ident):
        order = await self.orders.fresh(actor, ident)
        item_id = order["item"]["id"]
        revision, policy = self.get(item_id)
        reasons = []
        if not policy.enabled:
            reasons.append("политика выключена")
        if policy.mode == "manual":
            reasons.append("ручной режим")
        try:
            deliverable(order, ident, self.api.account.id)
        except ValidationError:
            reasons.append("состояние продажи не разрешает выдачу")
        if self.store.rows("SELECT 1 FROM order_annotations WHERE order_id=? AND problem=1", (ident,)):
            reasons.append("локальная проблема требует решения владельца")
        existing = self.store.rows(
            "SELECT kind,status,error_code FROM outbox WHERE target=? AND kind IN ('delivery','delivery_text','fulfill')",
            (ident,),
        )
        if any(
            row["kind"] in {"delivery", "delivery_text"}
            or row["status"] in {"PENDING", "PROCESSING", "UNCERTAIN", "RETRYABLE_FAILED"}
            for row in existing
        ):
            reasons.append("выдача уже зарегистрирована или есть незавершённая операция")
        if any(row["error_code"] == "MANUALLY_STOPPED_UNCERTAIN" for row in existing):
            reasons.append("внешний результат остановленной операции неизвестен")
        local = self.store.rows("SELECT status FROM orders WHERE id=?", (ident,))[0]
        if local["status"] != order["status"]:
            reasons.append("API и сохранённая история расходятся")
        chat = (order.get("chat") or {}).get("id")
        if chat:
            value = await self.api.call("get_chat", chat_id=chat)
            if value.get("id") != chat or value.get("is_texting_allowed") is not True:
                reasons.append("чат недоступен для отправки")
        available = self.store.rows(
            "SELECT COUNT(*) AS n FROM inventory WHERE item_id=? AND status='AVAILABLE'", (item_id,)
        )[0]["n"]
        if policy.mode == "stock" and available == 0:
            reasons.append("нет доступного уникального запаса")
        steps = []
        if not reasons:
            if policy.greeting:
                steps.append("greeting")
            steps.append("unique_stock" if policy.mode == "stock" else "fixed_text")
            if policy.after:
                steps.append("after")
            if policy.mark_sent:
                steps.append("seller_fulfillment_requires_separate_checks")
        result = {
            "dry_run": True,
            "runtime_enabled": False,
            "would_deliver": not reasons,
            "reasons": reasons,
            "steps": steps,
            "available": available,
            "policy_revision": revision,
            "writes": 0,
            "reservations_created": 0,
        }
        self.store.execute(
            "INSERT INTO automation_runs(order_id,policy_revision,actor,result,created_at) VALUES(?,?,?,?,?)",
            (ident, revision, actor, encode(result), now()),
        )
        return result
