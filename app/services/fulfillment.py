import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal

from app.database.store import Conflict, encode, now
from app.services.order_management import OrderManagement, deal_details, deliverable, order_snapshot
from app.services.products import ValidationError

# Closed orders cannot need another automatic completion. Keep the original
# operation in history and keep every other uncertain action actionable.
ACTIVE_UNKNOWN_SQL = (
    "NOT (coalesce(o.status,'') IN ('ROLLED_BACK','FAILED','CONFIRMED','CONFIRMED_AUTOMATICALLY') "
    "AND q.kind='fulfill' AND json_extract(q.payload,'$.mode') IS 'legacy' "
    "AND (json_extract(q.payload,'$.gift_proof') IS q.target OR "
    "(json_extract(q.payload,'$.chain_id') IS q.target AND "
    "(json_extract(q.payload,'$.platform_followup') IS 1 OR "
    "json_extract(q.payload,'$.delivery_proof') IS 1))))"
)


SELLER_CHECK = "Я передал(а) товар покупателю и выполнил(а) заказ"
KNOWN_ROLES = {
    "USER",
    "ACCOUNTANT",
    "ADMIN",
    "ADV_DIRECTOR",
    "ADV_MANAGER",
    "CHECKER",
    "DEVELOPER",
    "GAMES_AND_APPS",
    "MODERATOR",
    "MONITORING",
    "OFFICIAL_SELLER",
    "OFFICIAL_SELLER_ADMIN",
    "POSTMODERATOR",
    "POSTSECURITY",
    "SECURITY",
    "SUPPORT",
}
FIELDS = ("workerName", "telegram", "providerId", "supplierOrderNumber", "price", "currency")


def report_field(name, value):
    if name == "currency":
        if not isinstance(value, str) or value not in {"USD", "EUR", "KZT"}:
            raise ValidationError("Выберите валюту USD/EUR/KZT")
        return value
    if not isinstance(value, str):
        raise ValidationError("Некорректное поле отчёта: " + name)
    value = value.strip()
    if name == "price":
        price = value.replace(",", ".")
        if len(price) > 12 or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", price):
            raise ValidationError("Стоимость: число с точностью до двух знаков")
        amount = Decimal(price)
        if not Decimal("0.01") <= amount <= Decimal(100000):
            raise ValidationError("Стоимость от 0,01 до 100000")
        return format(amount, ".2f")
    limits = {
        "workerName": (2, 40),
        "telegram": (1, 64),
        "providerId": (1, 100),
        "supplierOrderNumber": (1, 64),
    }
    if name not in limits:
        raise ValidationError("Неизвестное поле отчёта")
    if name == "telegram":
        value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
        value = re.sub(r"^(?:www\.)?(?:t\.me|telegram\.me)/", "", value, flags=re.IGNORECASE).lstrip("@")
        value = re.split(r"[/?#\s]", value)[0]
        if not re.fullmatch(r"[A-Za-z0-9_]+", value):
            raise ValidationError("Введите Telegram username исполнителя")
    minimum, maximum = limits[name]
    if not minimum <= len(value.encode("utf-16-le")) // 2 <= maximum:
        raise ValidationError("Некорректная длина поля отчёта: " + name)
    return value


def report_fields(values):
    if not isinstance(values, dict) or set(values) != set(FIELDS):
        raise ValidationError("Нужны все поля отчёта об исполнении")
    return {name: report_field(name, values[name]) for name in FIELDS}


class Fulfillment:
    def __init__(self, store, api):
        self.store, self.api = store, api
        self.orders = OrderManagement(store, api)

    async def load(self, ident, *, platform=False):
        order = await deal_details(
            self.api, ident, need_chat=not platform, need_role=True, need_platform_data=platform
        )
        if platform:
            from app.services.platform_orders import platform_sale

            platform_sale(order, ident, self.api.account.id)
            if order["status"] != "PAID":
                raise ValidationError("Отметка продавца уже не требуется")
        else:
            deliverable(order, ident, self.api.account.id)
        self.orders.orders.ingest(order)
        if (order.get("item") or {}).get("seller_type") == "SYSTEM":
            if platform:
                from app.playerok.adapter import APIError

                raise APIError("PLATFORM_SYSTEM_FULFILLMENT")
            raise ValidationError("Системный товар выполняется средствами Playerok")
        role = order["item"]["user"].get("role")
        if not isinstance(role, str) or role not in KNOWN_ROLES:
            if platform:
                from app.playerok.adapter import APIError

                raise APIError("PLATFORM_ROLE_UNCONFIRMED")
            raise ValidationError("Роль продавца не подтверждена; старый способ не выбирается автоматически")
        mode = "legacy"
        if role == "OFFICIAL_SELLER":
            requirement = await self.api.call("get_fulfillment_requirement", deal_id=ident)
            value = requirement.get("requirement")
            if value not in {"none", "draft", "fields"}:
                raise ValidationError("Неизвестное требование отчёта; обновите данные")
            mode = "legacy" if value == "none" else value
            if mode != "legacy":
                entry = await self.api.call("get_fulfillment_entry", deal_id=ident)
                if entry.get("state") != "absent":
                    raise Conflict(
                        "Отчёт уже существует или его отсутствие не подтверждено; повтор не создаётся"
                    )
        if platform and mode != "legacy":
            from app.playerok.adapter import APIError

            raise APIError("PLATFORM_REPORT_REQUIRED")
        state = {"order": order_snapshot(order), "role": role, "mode": mode, "checklist": SELLER_CHECK}
        snapshot = hashlib.sha256(encode(state).encode()).hexdigest()
        return {"id": ident, "snapshot": snapshot, "mode": mode, "order": order}

    async def prepare(self, owner, ident):
        self.orders.owner(owner)
        if self.store.rows(
            "SELECT 1 FROM outbox WHERE target=? AND kind='fulfill' AND (status='UNCERTAIN' OR error_code='MANUALLY_STOPPED_UNCERTAIN')",
            (ident,),
        ):
            raise Conflict("Результат предыдущей отметки неизвестен; сначала сверьте операцию")
        return await self.load(ident)

    async def providers(self):
        value = await self.api.call("get_fulfillment_providers")
        items = value.get("items")
        if (
            not isinstance(items, list)
            or not items
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item.get("id")
                or not isinstance(item.get("name"), str)
                or not item.get("name")
                for item in items
            )
        ):
            raise ValidationError("Список поставщиков недоступен; произвольный поставщик не подставляется")
        return [{"id": item["id"], "name": item["name"]} for item in items]

    async def validate(self, prepared, values, attested):
        if attested is not True:
            raise ValidationError("Нужно лично подтвердить передачу товара покупателю")
        if prepared["mode"] != "fields":
            if values:
                raise ValidationError("Поля не относятся к текущему требованию")
            return {"state": "draft"} if prepared["mode"] == "draft" else {}
        fields = report_fields(values)
        providers = await self.providers()
        if not any(item["id"] == fields["providerId"] for item in providers):
            raise ValidationError("Выбранный поставщик больше недоступен")
        return {"state": "filled", **fields}

    async def queue(self, owner, ident, expected, values, attested, token):
        self.orders.owner(owner)
        if not self.api.account.writes_enabled:
            raise ValidationError("Записи Playerok отключены в локальном .env")
        prepared = await self.prepare(owner, ident)
        if prepared["snapshot"] != expected:
            raise Conflict("Сделка или требование отчёта изменились; начните заново")
        report = await self.validate(prepared, values, attested)
        return self.store.enqueue(
            "fulfill:" + ident + ":" + token,
            "fulfill",
            ident,
            {
                "snapshot": expected,
                "mode": prepared["mode"],
                "report": report,
                "attested": True,
                "actor": owner,
            },
            actor=owner,
        )

    async def result(self, action, *, observed=None):
        payload = json.loads(action["payload"]) if isinstance(action["payload"], str) else action["payload"]
        current = (
            observed if observed is not None else await self.api.call("get_deal", deal_id=action["target"])
        )
        owner = ((current.get("item") or {}).get("user") or {}).get("id")
        if (
            current.get("id") != action["target"]
            or current.get("direction") != "OUT"
            or owner != self.api.account.id
            or current.get("status") not in {"SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"}
        ):
            return None
        entry = {"state": "not_required"}
        if payload.get("mode") in {"draft", "fields"}:
            entry = await self.api.call("get_fulfillment_entry", deal_id=action["target"])
            report = payload["report"]
            try:
                submitted = datetime.fromisoformat(entry.get("submittedAt", ""))
                queued = datetime.fromisoformat(action["created_at"]).replace(microsecond=0)
                if (
                    entry.get("state") != report["state"]
                    or submitted.tzinfo is None
                    or submitted < queued
                    or (report["state"] == "filled" and entry.get("telegram") != report["telegram"])
                ):
                    return None
            except (TypeError, ValueError):
                return None
        elif payload.get("mode") != "legacy":
            return None
        self.orders.orders.ingest(current)
        return {
            "remote_id": action["target"],
            "remote_status": current["status"],
            "report_state": entry["state"],
        }


def automatic_legacy_completion(payload, target):
    return payload.get("mode") == "legacy" and (
        payload.get("gift_proof") == target
        or payload.get("chain_id") == target
        and (payload.get("platform_followup") is True or payload.get("delivery_proof") is True)
    )


def reconcile_observed_completion(store, db, order, seller):
    # This observes the requested state, not which request caused it.
    status, target = order["status"], order["id"]
    if (
        status not in {"SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"}
        or not seller
        or seller != store.setting("playerok:account_id")
    ):
        return
    rows = db.execute(
        "SELECT id,payload,status FROM outbox WHERE target=? AND kind='fulfill' AND started_at IS NOT NULL",
        (target,),
    ).fetchall()
    if len(rows) != 1 or rows[0]["status"] != "UNCERTAIN":
        return
    action = rows[0]
    payload = json.loads(action["payload"])
    if (
        not automatic_legacy_completion(payload, target)
        or not db.execute("SELECT 1 FROM administrators WHERE user_id=?", (payload.get("actor"),)).fetchone()
    ):
        return
    result = {
        "remote_id": target,
        "remote_status": status,
        "report_state": "not_required",
        "resolution_source": "SERVER_STATE",
    }
    db.execute(
        "UPDATE outbox SET status='SUCCEEDED',result=?,error_code=NULL,lease_token=NULL,lease_until=NULL "
        "WHERE id=? AND status='UNCERTAIN'",
        (encode(result), action["id"]),
    )
    db.execute(
        "UPDATE delivery_chains SET status='SUCCEEDED' WHERE order_id=? AND status='BLOCKED' "
        "AND EXISTS(SELECT 1 FROM delivery_chain_steps WHERE order_id=? AND operation_id=? AND kind='fulfill') "
        "AND NOT EXISTS(SELECT 1 FROM delivery_chain_steps s LEFT JOIN outbox o ON o.id=s.operation_id "
        "WHERE s.order_id=? AND coalesce(o.status,'')<>'SUCCEEDED')",
        (target, target, action["id"], target),
    )
    db.execute(
        "INSERT INTO order_history(order_id,source,new_status,note,created_at) VALUES(?,'AUTO_FULFILLMENT_RESOLUTION',?,'SERVER_STATE',?)",
        (target, status, now()),
    )
