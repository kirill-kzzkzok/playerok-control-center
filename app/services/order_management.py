import hashlib
import json
from decimal import Decimal, InvalidOperation

from app.database.store import Conflict, encode
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


def deliverable(order, ident, account_id):
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
        raise ValidationError("Чат покупателя не подтверждён")
    return order


async def deal_details(api, ident, *, full_item=False, need_chat=True):
    """Persisted/list deals may omit chat and item fields; omissions are not refusals.

    Read the SAME deal through the full contract before applying delivery guards.
    Never infer a chat or switch a purchased item to a different public listing.
    """
    order = await api.call("get_deal", deal_id=ident)
    sale(order, ident, api.account.id)
    item = order.get("item") or {}
    if (
        need_chat
        and not (order.get("chat") or {}).get("id")
        or full_item
        and (not item.get("description") or not (item.get("category") or {}).get("id"))
    ):
        detailed = await api.call("get_delivery_deal", deal_id=ident)
        sale(detailed, ident, api.account.id)
        if (detailed.get("item") or {}).get("id") != item["id"]:
            raise Conflict("Полная сделка относится к другому товару; выдача остановлена")
        detailed_item = detailed.get("item") or {}
        if full_item and item.get("price") is not None and detailed_item.get("price") is not None:
            try:
                short_price, full_price = Decimal(str(item["price"])), Decimal(str(detailed_item["price"]))
            except InvalidOperation:
                raise Conflict("Playerok не подтвердил цену покупки; выдача остановлена") from None
            if not short_price.is_finite() or not full_price.is_finite() or short_price != full_price:
                raise Conflict(
                    "Цена в двух карточках одной сделки различается. Обновите заказ; выдача остановлена"
                )
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

    async def prepare_delivery(self, actor, ident):
        order = await self.fresh(actor, ident)
        deliverable(order, ident, self.api.account.id)
        local = self.store.rows("SELECT status FROM orders WHERE id=?", (ident,))[0]
        if local["status"] != order["status"]:
            raise Conflict("API и сохранённая история расходятся; выдача заблокирована")
        chat = await self.api.call("get_chat", chat_id=order["chat"]["id"])
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
