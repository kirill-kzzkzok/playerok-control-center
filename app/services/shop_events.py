import json
import re
import unicodedata
from html import escape
from urllib.parse import quote

from app.security.logging import event

STAGES = {
    "PAID": ("🛒", "ПОКУПКА", "Новая покупка", "⏳ Ожидает выдачи"),
    "GIFT_DELIVERED": ("🎁", "ВЫДАНО", "Товар выдан", "💚 Автовыдача завершена успешно!"),
    "DELIVERED": ("🎁", "ВЫДАНО", "Товар выдан", "💚 Автовыдача завершена успешно!"),
    "SENT": ("✅", "ВЫПОЛНЕНО", "Заказ выполнен", "✨ Playerok подтвердил выполнение заказа!"),
    "CONFIRMED": ("💚", "ПОЛУЧЕНО", "Заказ получен", "✨ Покупатель подтвердил получение!"),
    "CONFIRMED_AUTOMATICALLY": (
        "✅",
        "ЗАВЕРШЕНО",
        "Заказ завершён автоматически",
        "✨ Playerok завершил заказ!",
    ),
    "ROLLED_BACK": ("↩️", "ВОЗВРАТ", "Возврат подтверждён", "💰 Playerok подтвердил возврат средств."),
    "FAILED": ("↩️", "ОТМЕНА", "Заказ отменён", ""),
}


def short_title(value):
    text = str(value or "")
    text = re.sub(r"\[[A-Z0-9]{3}\]|\b(?:авто)?выдача\s*24\s*/\s*7\b", " ", text, flags=re.IGNORECASE)
    text = "".join(c for c in text if unicodedata.category(c)[0] in "LNPZ" and c not in "|️")
    return " ".join(text.split())[:65].strip(" -·") or "Товар"


def buyer(order):
    name = (order.get("user") or {}).get("username")
    return name.strip() if isinstance(name, str) and name.strip() else "Имя не указано"


def product_title(name):
    return name if isinstance(name, str) and name.strip() else "Товар"


def order_title(order):
    return product_title((order.get("item") or {}).get("name"))


def stock_title(db, item_id, order_id=None):
    row = db.execute(
        "SELECT COALESCE((SELECT json_extract(payload,'$.item.name') FROM orders WHERE item_id=? "
        "ORDER BY (id IS ?) DESC,created_at DESC LIMIT 1),"
        "(SELECT title_snapshot FROM logical_listings WHERE item_id=?))",
        (item_id, order_id, item_id),
    ).fetchone()
    return product_title(row[0])


def amount(order):
    value = (order.get("item") or {}).get("price")
    return f"{value} ₽" if re.fullmatch(r"\d+(?:\.\d{1,2})?", str(value)) else ""


def order_event(order, stage):
    icon, label, _, _ = STAGES[stage]
    name = f"«{order_title(order)}»"
    customer = buyer(order)
    message = {
        "PAID": f"{customer} купил {name}",
        "DELIVERED": f"Товар {name} успешно выдан · {customer}",
        "GIFT_DELIVERED": f"Товар {name} успешно выдан · {customer}",
        "SENT": f"Заказ {customer} выполнен на Playerok · {name}",
        "CONFIRMED": f"{customer} подтвердил получение {name}",
        "CONFIRMED_AUTOMATICALLY": f"Playerok автоматически завершил заказ {customer} · {name}",
        "ROLLED_BACK": f"Playerok подтвердил возврат · {customer} · {name}",
        "FAILED": f"Заказ отменён · {customer} · {name}",
    }[stage]
    if stage == "PAID" and (price := amount(order)):
        message += " · " + price
    event(
        icon + " " + label,
        ("💚" if stage in {"DELIVERED", "GIFT_DELIVERED"} else icon) + " " + message,
        order=order["id"],
        success=stage in {"DELIVERED", "GIFT_DELIVERED", "SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"},
    )


def notice_heading(icon, title):
    return f"{icon} <b>{escape(title.upper())}</b>\n━━━━━━━━━━━━━━━━━━\n"


def order_notice(order, stage, review=None, *, delivered_before_refund=None):
    product = "📦 " + escape(order_title(order))
    customer = "👤 " + escape(buyer(order))
    if review:
        rating = review["rating"]
        lines = [notice_heading("⭐", "Новый отзыв"), customer, product, "", f"{'⭐' * rating} {rating}/5"]
        if text := str(review.get("text") or "").strip()[:700]:
            lines.extend(("", "💬 " + escape(text)))
        return with_order_link("\n".join(lines), order["id"])
    icon, _, title, detail = STAGES[stage]
    if stage == "PAID" and order.get("status") != "PAID":
        detail = "Оплата подтверждена."
    lines = [notice_heading(icon, title), product, customer]
    if stage in {"PAID", "SENT", "CONFIRMED_AUTOMATICALLY"} and (price := amount(order)):
        lines.append("💰 " + ("+" if stage == "PAID" else "") + price)
    if detail:
        lines.extend(("", detail))
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
    body = (
        f"{notice_heading('⚠️', 'Проблема с заказом')}\n"
        f"📦 {escape(order_title(order))}\n👤 {escape(buyer(order))}\n\n"
        f"❌ {escape(title)}\n{escape(detail)}"
    )
    return with_order_link(body, order["id"])


def with_order_link(body, ident):
    url = "https://playerok.com/deal/" + quote(ident, safe="")
    return body.rstrip() + f'\n\n🔗 <a href="{url}">Открыть заказ</a>'
