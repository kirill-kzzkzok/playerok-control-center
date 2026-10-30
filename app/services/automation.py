import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

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


def future_order(created_at, armed_at):
    try:
        created, armed = datetime.fromisoformat(created_at), datetime.fromisoformat(armed_at)
        return (
            created.tzinfo is not None
            and armed.tzinfo is not None
            and armed <= created <= datetime.now(UTC) + timedelta(minutes=5)
        )
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class DeliveryPolicy:
    enabled: bool = False
    mode: str = "manual"
    text: str = ""
    greeting: str = ""
    after: str = ""
    mark_sent: bool = False
    auto_enroll: bool = False

    def __post_init__(self):
        if (
            type(self.enabled) is not bool
            or type(self.mark_sent) is not bool
            or type(self.auto_enroll) is not bool
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

    def readiness(self, ident):
        """Shared local snapshot; this screen never enables execution."""
        with self.store.transaction():
            revision, policy = self.get(ident)
            rows = self.store.rows(
                "SELECT status,COUNT(*) AS n FROM inventory WHERE item_id=? GROUP BY status", (ident,)
            )
            counts = {
                s: next((r["n"] for r in rows if r["status"] == s), 0)
                for s in ("AVAILABLE", "RESERVED", "DELIVERED", "QUARANTINED")
            }
            guard = self.store.setting(f"delivery:{ident}:enabled")
            baseline = bool(self.store.setting("orders:last_complete_sync"))
            sync = self.store.setting("sync:health", {}).get("status", "NOT_STARTED")
        writes = bool(getattr(self.api.account, "writes_enabled", False))
        runtime = bool(getattr(self.api.account, "automation_enabled", False))
        reasons = []
        if not revision:
            reasons.append("политика ещё не настроена")
        elif not policy.enabled:
            reasons.append("политика отключена владельцем")
        if guard is False:
            reasons.append("защитная остановка; требуется явное возобновление")
        if policy.mode == "manual":
            reasons.append("ручной режим")
        if not policy.auto_enroll:
            reasons.append("будущие заказы автоматически не включаются")
        if not writes:
            reasons.append("глобальные записи выключены")
        if not runtime:
            reasons.append("глобальная автоматика выключена")
        if not baseline:
            reasons.append("первоначальная сверка заказов не завершена")
        if sync == "ERROR":
            reasons.append("последняя сверка завершилась ошибкой")
        if not getattr(self.api.account, "id", None):
            reasons.append("сессия Playerok не подтверждена")
        if policy.mode == "stock" and not counts["AVAILABLE"]:
            reasons.append("нет доступного запаса")
        stopped = (revision and not policy.enabled) or guard is False
        return {
            "status": "Остановлено" if stopped else "Не готово" if reasons else "Работает",
            "mode": policy.mode,
            "revision": revision,
            "counts": counts,
            "reasons": reasons,
            "writes": writes,
            "runtime": runtime,
            "sync": sync,
            "baseline": baseline,
        }

    def status_text(self, ident):
        if self.store.rows("SELECT 1 FROM gift_listings WHERE item_id=?", (ident,)):
            from app.gifts.service import GiftService
            from app.gifts.settings import GiftSettings

            return GiftService(
                self.store, self.api, getattr(self.api, "gift_settings", None) or GiftSettings.load()
            ).status_text(ident)
        v = self.readiness(ident)
        mode = {"manual": "Ручной", "text": "Постоянный текст", "stock": "Уникальный запас"}[v["mode"]]
        text = f"⚡ Автоматизация: {v['status']}\nДействующий режим: {mode} · политика v{v['revision']}"
        text += f"\nЗапас: {v['counts']['AVAILABLE']} доступно · {v['counts']['RESERVED']} в резерве · {v['counts']['DELIVERED']} выдано · {v['counts']['QUARANTINED']} в карантине"
        text += f"\nГлобальные разрешения: записи {'да' if v['writes'] else 'нет'}, автоматика {'да' if v['runtime'] else 'нет'}"
        text += f"\nСверка: {v['sync']} · первоначальная {'завершена' if v['baseline'] else 'не завершена'}"
        text += "\nПричины: " + (
            "; ".join(v["reasons"]) or "нет; готово к подходящим будущим оплачиваемым заказам"
        )
        if v["sync"] == "ERROR":
            text += "\n⚠️ Последняя сверка с ошибкой; проверьте актуальность данных."
        return text + "\nSENT — только после отдельного подтверждения владельца."

    async def save(self, actor, ident, expected, payload, *, future_confirmed=False, stock_resume=False):
        policy = DeliveryPolicy(**payload)
        if policy.enabled and policy.auto_enroll and future_confirmed is not True:
            raise ValidationError("Нужно отдельно подтвердить автоматическую выдачу будущим заказам")
        item = await self.item(actor, ident)
        if stock_resume and (
            item.get("deleted_at")
            or item.get("status") not in {"DRAFT", "APPROVED", "PENDING_APPROVAL", "PENDING_MODERATION"}
        ):
            raise Conflict("Состояние лота больше не разрешает возобновление")
        with self.store.transaction() as db:
            row = db.execute("SELECT revision FROM delivery_policies WHERE item_id=?", (ident,)).fetchone()
            if (
                policy.enabled
                and db.execute(
                    "SELECT 1 FROM gift_listings WHERE item_id=? AND enabled=1", (ident,)
                ).fetchone()
            ):
                raise Conflict("Сначала остановите подарочную выдачу для этого лота")
            revision = row[0] if row else 0
            if revision != expected:
                raise Conflict("Настройки изменились в другом сценарии. Откройте их заново")
            if stock_resume and (
                not policy.enabled
                or policy.mode != "stock"
                or self.store.setting(f"delivery:{ident}:enabled") is not False
                or not db.execute(
                    "SELECT 1 FROM inventory WHERE item_id=? AND status='AVAILABLE' LIMIT 1", (ident,)
                ).fetchone()
            ):
                raise Conflict("Возобновление больше недоступно: проверьте остановку и остаток")
            db.execute(
                "INSERT INTO delivery_policies VALUES(?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET revision=excluded.revision,actor=excluded.actor,payload=excluded.payload,updated_at=excluded.updated_at",
                (ident, revision + 1, actor, encode(asdict(policy)), now()),
            )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'SAVE_DELIVERY_POLICY',?,?,?)",
                (
                    actor,
                    ident,
                    "FUTURE_ORDERS_ARMED" if policy.enabled and policy.auto_enroll else "LOCAL_POLICY_SAVED",
                    now(),
                ),
            )
            db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"delivery:{ident}:enabled", encode(policy.enabled)),
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
