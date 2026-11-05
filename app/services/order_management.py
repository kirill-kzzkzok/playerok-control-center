import hashlib
import json
from decimal import Decimal, InvalidOperation

from app.database.store import Conflict, encode, server_time
from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.services.products import ValidationError


def sale(order, ident, account_id):
    item = order.get("item") or {}
    seller = (item.get("user") or {}).get("id")
    if (
        order.get("id") != ident
        or order.get("direction") != "OUT"
        or not item.get("id")
        or not order.get("status")
    ):
        raise ValidationError("Доступна только полная карточка продажи")
    if seller and seller != account_id:
        raise ValidationError("Продавец сделки не соответствует сессии")
    return order


def deliverable(order, ident, account_id, *, wait_for_chat=False):
    sale(order, ident, account_id)
    if not account_id or ((order.get("item") or {}).get("user") or {}).get("id") != account_id:
        raise ValidationError("Владелец товара не подтверждён")
    if (
        order.get("status") != "PAID"
        or order.get("has_problem") is not False
        or order.get("is_automated") is not False
    ):
        raise ValidationError("Нужна оплаченная продажа без проблемы и серверной автоматической выдачи")
    if not (order.get("chat") or {}).get("id"):
        if wait_for_chat:
            raise APIError("DELIVERY_CHAT_NOT_YET_AVAILABLE")
        raise ValidationError("Чат покупателя не подтверждён")
    return order


def compatible_detail(short, detailed):
    """Partial observations may omit data, but conflicting evidence is not merged."""
    old, new = short.get("item") or {}, detailed.get("item") or {}
    for left, right, label in (
        (old.get("id"), new.get("id"), "товар"),
        ((short.get("user") or {}).get("id"), (detailed.get("user") or {}).get("id"), "покупатель"),
        ((short.get("chat") or {}).get("id"), (detailed.get("chat") or {}).get("id"), "чат"),
        ((old.get("user") or {}).get("id"), (new.get("user") or {}).get("id"), "продавец"),
        ((old.get("category") or {}).get("id"), (new.get("category") or {}).get("id"), "категория"),
        (
            (old.get("obtaining_type") or {}).get("id"),
            (new.get("obtaining_type") or {}).get("id"),
            "способ получения",
        ),
    ):
        if left and right and left != right:
            raise Conflict("Карточки одной сделки содержат разные данные: " + label)
    for field in ("name", "description", "attributes", "data_fields", "comment"):
        if old.get(field) is not None and new.get(field) is not None and old[field] != new[field]:
            raise Conflict("Критичные параметры в карточках одной сделки расходятся")
    if old.get("price") is not None and new.get("price") is not None:
        try:
            before, after = Decimal(str(old["price"])), Decimal(str(new["price"]))
        except InvalidOperation:
            raise Conflict("Playerok не подтвердил цену покупки; выдача остановлена") from None
        if not before.is_finite() or not after.is_finite() or before != after:
            raise Conflict(
                "Цена в двух карточках одной сделки различается. Обновите заказ; выдача остановлена"
            )
    if short.get("status") in {
        "SENT",
        "CONFIRMED",
        "CONFIRMED_AUTOMATICALLY",
        "ROLLED_BACK",
        "FAILED",
    } and detailed.get("status") in {"PENDING", "PAID"}:
        raise Conflict("Полная карточка вернула более ранний статус сделки")
    if short.get("has_problem") is True and detailed.get("has_problem") is not True:
        raise Conflict("Карточки сделки расходятся по наличию проблемы. Выполнение остановлено")
    left = short.get("obtaining_fields")
    right = detailed.get("obtaining_fields")
    if isinstance(left, list) and isinstance(right, list):
        for field in left:
            if not isinstance(field, dict) or field.get("value") in (None, ""):
                continue
            matches = [v for v in right if isinstance(v, dict) and v.get("id") == field.get("id")]
            if any(v.get("value") is not None and v["value"] != field["value"] for v in matches):
                raise Conflict("Данные получателя в карточках одной сделки расходятся")


async def deal_details(
    api,
    ident,
    *,
    full_item=False,
    need_chat=True,
    obtaining_field_id=None,
    need_created=False,
    need_role=False,
    force_full=False,
):
    """Persisted/list deals may omit chat and item fields; omissions are not refusals.

    Read the SAME deal through the full contract before applying delivery guards.
    Never infer a chat or switch a purchased item to a different public listing.
    """
    order = await api.call("get_deal", deal_id=ident)
    sale(order, ident, api.account.id)
    item = order.get("item") or {}
    fields = order.get("obtaining_fields")
    has_field = isinstance(fields, list) and any(
        isinstance(f, dict) and f.get("id") == obtaining_field_id and f.get("value") for f in fields
    )
    if (
        force_full
        or need_chat
        and not (order.get("chat") or {}).get("id")
        or full_item
        and (
            item.get("description") is None
            or not (item.get("category") or {}).get("id")
            or not (item.get("obtaining_type") or {}).get("id")
        )
        or not (item.get("user") or {}).get("id")
        or obtaining_field_id
        and not has_field
        or need_created
        and server_time(order.get("created_at")) is None
        or need_role
        and not (item.get("user") or {}).get("role")
        or order.get("has_problem") is None
        or order.get("is_automated") is None
    ):
        detailed = await api.call("get_delivery_deal", deal_id=ident)
        sale(detailed, ident, api.account.id)
        if (detailed.get("item") or {}).get("id") != item["id"]:
            raise Conflict("Полная сделка относится к другому товару; выдача остановлена")
        compatible_detail(order, detailed)
        order = detailed
    return order


def order_snapshot(order):
    value = {key: order.get(key) for key in ("id", "direction", "status", "has_problem", "is_automated")}
    value.update(item_id=(order.get("item") or {}).get("id"), chat_id=(order.get("chat") or {}).get("id"))
    return hashlib.sha256(encode(value).encode()).hexdigest()


class OrderManagement:
    def __init__(self, store, api):
        self.store, self.api, self.orders = store, api, Orders(store)

    def owner(self, actor):
        if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (actor,)):
            raise Conflict("Доступ закрыт")

    async def fresh(self, actor, ident):
        self.owner(actor)
        order = await self.api.call("get_deal", deal_id=ident)
        sale(order, ident, getattr(self.api.account, "id", None))
        self.orders.ingest(order)
        return order

    async def prepare_delivery(self, actor, ident, *, automatic=False):
        self.owner(actor)
        order = await deal_details(self.api, ident)
        self.orders.ingest(order)
        local = self.store.rows("SELECT status FROM orders WHERE id=?", (ident,))[0]
        deliverable(
            order, ident, self.api.account.id, wait_for_chat=automatic and local["status"] == order["status"]
        )
        if local["status"] != order["status"]:
            raise Conflict("API и сохранённая история расходятся; выдача заблокирована")
        chat = await self.api.call("get_chat", chat_id=order["chat"]["id"])
        if automatic and chat.get("id") == order["chat"]["id"] and chat.get("is_texting_allowed") is None:
            raise APIError("DELIVERY_CHAT_NOT_YET_AVAILABLE")
        if chat.get("id") != order["chat"]["id"] or chat.get("is_texting_allowed") is not True:
            raise ValidationError("Возможность отправки в чат не подтверждена")
        return order

    async def queue_delivery(self, actor, ident, expected):
        if not self.api.account.writes_enabled:
            raise ValidationError("Записи Playerok отключены в локальном .env")
        order = await self.prepare_delivery(actor, ident)
        if order_snapshot(order) != expected:
            raise Conflict("Сделка изменилась после превью. Откройте заказ заново")
        stock = self.orders.reserve(ident, actor=actor)
        if stock is None:
            raise Conflict("Запас пуст. Пустое сообщение не отправлено; автовыдача выключена")
        return self.store.rows("SELECT id FROM outbox WHERE operation_key=?", ("delivery:" + ident,))[0]["id"]

    async def add_stock(self, actor, ident, expected, text):
        self.owner(actor)
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise ValidationError("Введите уникальные коды по одному на строку, до 10000 символов")
        order = await self.fresh(actor, ident)
        if order_snapshot(order) != expected:
            raise Conflict("Сделка изменилась. Откройте заказ заново")
        count = self.orders.add_stock(order["item"]["id"], text.splitlines())
        self.store.audit(actor, "ADD_STOCK", order["item"]["id"], str(count))
        return count

    def local(self, ident):
        rows = self.store.rows("SELECT * FROM order_annotations WHERE order_id=?", (ident,))
        return rows[0] if rows else {"problem": 0, "note": "", "revision": 0}

    def mode(self, item_id):
        rows = self.store.rows(
            "SELECT d.payload FROM drafts d JOIN bindings b ON b.draft_id=d.id WHERE b.remote_id=?",
            (item_id,),
        )
        selected = json.loads(rows[0]["payload"]).get("delivery", "manual") if rows else "manual"
        return {
            "manual": "Ручной",
            "stock": "Уникальный запас (настройка черновика)",
            "text": "Постоянный текст (настройка черновика)",
        }.get(selected, "Неизвестен")
