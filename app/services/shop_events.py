import json
import re
import unicodedata
from html import escape
from urllib.parse import quote

from app.security.logging import event

STAGES = {
    "PAID": ("🛒", "ПОКУПКА", "Новая покупка", "Ожидает выдачи"),
    "GIFT_DELIVERED": ("🎁", "ВЫДАНО", "Подарок отправлен", "Автовыдача выполнена"),
    "DELIVERED": ("🎁", "ВЫДАНО", "Товар выдан", "Автовыдача выполнена"),
    "SENT": ("✅", "ВЫПОЛНЕНО", "Заказ выполнен", "Статус подтверждён Playerok"),
    "CONFIRMED": ("💚", "ПОЛУЧЕНО", "Покупатель подтвердил получение", ""),
    "CONFIRMED_AUTOMATICALLY": ("✅", "ЗАВЕРШЕНО", "Заказ завершён автоматически", ""),
    "ROLLED_BACK": ("↩️", "ВОЗВРАТ", "Возврат подтверждён", ""),
    "FAILED": ("↩️", "ОТМЕНА", "Заказ отменён", ""),
}


def short_title(value):
    text = str(value or "")
    for pattern, label in (
        (r"мишк", "Мишка"),
        (r"сердечк", "Сердечко"),
        (r"steam.*казахстан", "Steam Казахстан"),
    ):
        if re.search(pattern, text, re.IGNORECASE):
            return label
    text = re.sub(r"\[[A-Z0-9]{3}\]|\b(?:авто)?выдача\s*24\s*/\s*7\b", " ", text, flags=re.IGNORECASE)
    text = "".join(c for c in text if unicodedata.category(c)[0] in "LNPZ" and c not in "|️")
    return " ".join(text.split())[:65].strip(" -·") or "Товар"


def buyer(order):
    name = (order.get("user") or {}).get("username")
    return name if isinstance(name, str) and re.fullmatch(r"[\w.-]{1,48}", name) else "Имя не указано"


def amount(order):
    value = (order.get("item") or {}).get("price")
    return f"{value} ₽" if re.fullmatch(r"\d+(?:\.\d{1,2})?", str(value)) else ""


def order_event(order, stage):
    icon, label, _, detail = STAGES[stage]
    name = short_title((order.get("item") or {}).get("name"))
    parts = [name, buyer(order)]
    if stage in {"DELIVERED", "GIFT_DELIVERED", "SENT"}:
        parts.append(detail)
    if stage == "PAID" and amount(order):
        parts.insert(0, "+" + amount(order))
    event(
        icon + " " + label,
        " · ".join(parts),
        order=order["id"],
        success=stage in {"DELIVERED", "GIFT_DELIVERED", "SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"},
    )


def order_notice(order, stage, review=None, *, delivered_before_refund=None):
    name = short_title((order.get("item") or {}).get("name"))
    if review:
        icon, title, detail = "⭐", f"Новый отзыв · {review['rating']}/5", str(review.get("text") or "")[:700]
    else:
        icon, _, title, detail = STAGES[stage]
        if stage == "PAID" and amount(order):
            title += " · +" + amount(order)
    lines = [f"{icon} <b>{title}</b>", "", escape(name), "👤 Покупатель: " + escape(buyer(order))]
    if detail:
        lines.append(escape(detail))
    if stage == "CONFIRMED" and amount(order):
        lines.append("💰 Сумма: " + amount(order))
    if stage == "ROLLED_BACK" and delivered_before_refund is not None:
        lines.append(
            "🎁 Подарок был отправлен до возврата."
            if delivered_before_refund
            else "📦 Товар был выдан до возврата."
        )
    return with_order_link("\n".join(lines), order["id"])


def record_delivery(store, ident, *, gift=False):
    from app.database.store import encode, now

    rows = store.rows("SELECT payload FROM orders WHERE id=?", (ident,))
    if not rows:
        return
    at = now()
    if store.execute(
        "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) VALUES(?,'order_delivered',?,?,?)",
        ("order-delivered:" + ident, encode({"id": ident, "created_at": at, "gift": gift}), at, at),
    ).rowcount:
        order_event(json.loads(rows[0]["payload"]), "GIFT_DELIVERED" if gift else "DELIVERED")


def order_alert(order, title, detail):
    name = short_title((order.get("item") or {}).get("name"))
    body = (
        f"⚠️ <b>{escape(title)}</b>\n\n{escape(name)}\n👤 Покупатель: {escape(buyer(order))}\n{escape(detail)}"
    )
    return with_order_link(body, order["id"])


def with_order_link(body, ident):
    url = "https://playerok.com/deal/" + quote(ident, safe="")
    return body.rstrip() + f'\n\n<a href="{url}">Открыть заказ</a>'
