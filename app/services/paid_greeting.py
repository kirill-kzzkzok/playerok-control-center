import re

from app.database.store import Conflict, now, server_time
from app.gifts.core import USERNAME
from app.services.order_management import deal_details
from app.services.shop_events import short_title

FRAME = "୨୧ ── ⋆｡˚ ♡ ˚｡⋆ ── ୨୧"


def greeting_key(account, order_id):
    return f"paid-greeting:{account}:{order_id}"


def unstarted_recipient_request(db, ident):
    return bool(
        db.execute(
            "SELECT 1 FROM outbox o WHERE o.id=? AND o.kind='reply' AND o.started_at IS NULL "
            "AND (o.result IS NULL OR o.result='null') "
            "AND json_extract(o.payload,'$.paid_greeting.recipient_request')=1 "
            "AND NOT EXISTS(SELECT 1 FROM outbox_late_results r WHERE r.operation_id=o.id) "
            "AND NOT EXISTS(SELECT 1 FROM operation_resolutions r WHERE r.operation_id=o.id)",
            (ident,),
        ).fetchone()
    )


def enqueue(store, db, order, seller):
    account = store.setting("playerok:account_id")
    cutoff = store.setting("orders:greeting_since")
    created = server_time(order.get("created_at"))
    if not (
        account
        and seller == account
        and cutoff
        and created
        and cutoff <= created <= now()
        and order["status"] == "PAID"
        and order.get("has_problem") is False
        and not order.get("_created_at_conflict")
    ):
        return
    buyer = (order.get("user") or {}).get("id")
    chat = (order.get("chat") or {}).get("id")
    if not buyer or buyer == account or not chat:
        return
    if db.execute(
        "SELECT 1 FROM delivery_chains c JOIN delivery_chain_steps s ON s.order_id=c.order_id "
        "WHERE c.order_id=? AND s.kind='chain_message' AND s.position=0 "
        "AND s.text=json_extract(c.policy,'$.greeting')",
        (order["id"],),
    ).fetchone():
        return
    store.enqueue(
        greeting_key(account, order["id"]),
        "reply",
        chat,
        {
            "paid_greeting": {
                "account": account,
                "order": order["id"],
                "item": order["item"]["id"],
                "buyer": buyer,
            }
        },
        db,
    )


def render(order, *, username_field_id=None):
    item = order["item"]
    fields = {}
    for field in item.get("data_fields") or []:
        if isinstance(field, dict) and field.get("type") == "OBTAINING_DATA" and field.get("id"):
            fields[field["id"]] = {**field, "value": None}
    for field in order.get("obtaining_fields") or []:
        if isinstance(field, dict) and field.get("id"):
            fields[field["id"]] = {**fields.get(field["id"], {}), **field}
    filled, missing = [], []
    for ident, field in list(fields.items())[:12]:
        label = field.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        label = " ".join(label.split())[:80]
        if re.search(
            r"парол|password|токен|token|секрет|secret|ключ|key|логин|login|код доступа", label, re.IGNORECASE
        ):
            continue
        value = field.get("value")
        if isinstance(value, str) and value.strip():
            value = value.strip()
            public = re.fullmatch(r"\btelegram\b|\b(?:ник(?:нейм)?|nickname)\b", label, re.IGNORECASE)
            if ident == username_field_id or re.search(r"(?<!\w)@username\b", label, re.IGNORECASE):
                match = USERNAME.fullmatch(value)
                filled.append(f"@username: @{match.group(1)}" if match else "@username: указано")
            else:
                shown = (
                    value if public and re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_.-]{2,31}", value) else "указано"
                )
                filled.append(f"{label}: {shown}")
        elif field.get("required") is True:
            missing.append("@username" if ident == username_field_id else label)
    lines = [
        FRAME,
        "",
        "спасибо за покупку! ♡",
        "",
        "🎁 твой заказ:",
        short_title(item.get("name")),
        "",
        "✨ оплата прошла успешно!",
        "заказ принят в обработку ♡",
    ]
    if missing:
        lines += [
            "",
            "💌 для выполнения заказа укажи, пожалуйста:",
            *missing,
            "",
            "отправь данные в этот чат ♡",
        ]
    elif filled:
        lines += ["", "📝 данные заказа:", *filled]
    return "\n".join([*lines, "", FRAME])


def guard(store, api, action):
    proof = action["payload"]["paid_greeting"]
    if api.account.id != proof["account"] or store.setting("playerok:account_id") != proof["account"]:
        raise Conflict("Аккаунт приветствия изменился")
    local = store.rows("SELECT status,chat_id,item_id FROM orders WHERE id=?", (proof["order"],))
    if (
        not local
        or local[0]["status"] not in {"PAID", "SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"}
        or (local[0]["chat_id"], local[0]["item_id"]) != (action["target"], proof["item"])
    ):
        raise Conflict("Сделка приветствия изменилась")
    if proof.get("recipient_request"):
        from app.gifts.service import GiftService
        from app.services.logical_products import LogicalProducts

        service = GiftService(store, api, api.gift_settings, api.gift_sender)
        job = service.job(proof["order"])
        listing = service.listing(proof["item"])
        if (
            local[0]["status"] != "PAID"
            or job["state"] not in {"PENDING", "CHECKING"}
            or job["spent_at"]
            or job["form_id"]
            or job["receipt"]
            or job["operation_id"] != proof.get("gift_operation")
            or not service.runtime()
            or not listing["enabled"]
            or listing["revision"] != job["listing_revision"]
        ):
            raise Conflict("Запрос получателя больше не разрешён")
        LogicalProducts(store, api).require_delivery(proof["item"], "telegram_gifts", proof["order"])
    return proof


async def prepare(store, api, action):
    proof = guard(store, api, action)
    order = await deal_details(api, proof["order"], full_item=True, need_created=True, force_full=True)
    if (
        order.get("status") not in {"PAID", "SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"}
        or order.get("has_problem") is not False
        or order["item"]["id"] != proof["item"]
        or (order["item"].get("user") or {}).get("id") != proof["account"]
        or (order.get("user") or {}).get("id") != proof["buyer"]
        or (order.get("chat") or {}).get("id") != action["target"]
    ):
        raise Conflict("Сделка или покупатель изменились")
    chat = await api.call("get_chat", chat_id=action["target"])
    if chat.get("id") != action["target"] or chat.get("is_texting_allowed") is not True:
        raise Conflict("Чат покупателя недоступен")
    configured = store.rows("SELECT username_field_id FROM gift_listings WHERE item_id=?", (proof["item"],))
    if not configured:
        from app.services.product_codes import title_code

        try:
            code = title_code(order["item"].get("name"))
        except ValueError:
            code = None
        configured = store.rows(
            "SELECT DISTINCT g.username_field_id FROM logical_products p "
            "JOIN logical_listings l ON l.product_id=p.id JOIN gift_listings g ON g.item_id=l.item_id "
            "WHERE p.account_id=? AND p.code=? AND p.handler='telegram_gifts' "
            "AND p.state='VERIFIED' AND p.enabled=1 AND l.verified_at IS NOT NULL AND g.enabled=1",
            (proof["account"], code),
        )
        if len(configured) != 1:
            configured = []
    if configured:
        field_id = configured[0]["username_field_id"]
        fields = list(order.get("obtaining_fields") or [])
        matching = [field for field in fields if isinstance(field, dict) and field.get("id") == field_id]
        raw = matching[0].get("value") if len(matching) == 1 else None
        invalid = bool(raw) and not (isinstance(raw, str) and USERNAME.fullmatch(raw.strip()))
        if not any(isinstance(field, dict) and field.get("id") == field_id for field in fields):
            fields.append({"id": field_id, "label": "Telegram", "required": True})
        else:
            fields = [
                {
                    **field,
                    "label": field.get("label") or "Telegram",
                    "required": True,
                    "value": None if invalid else field.get("value"),
                }
                if isinstance(field, dict) and field.get("id") == field_id
                else field
                for field in fields
            ]
        order = {**order, "obtaining_fields": fields}
        missing = not any(f.get("id") == field_id and f.get("value") for f in fields if isinstance(f, dict))
        action["recipient_requested"] = missing
        if proof.get("recipient_request"):
            if not missing:
                return None
            from app.gifts.mtproto import GiftFailure
            from app.gifts.service import GiftService

            try:
                await GiftService(store, api, api.gift_settings, api.gift_sender).recipient_username(
                    order, {"username_field_id": field_id}, request=False
                )
            except GiftFailure as error:
                if error.code != "RECIPIENT_DATA_PENDING":
                    raise
            else:
                return None
            return (
                f"{FRAME}\n\n"
                + ("💌 не получилось распознать username из заказа.\n" if invalid else "")
                + "чтобы отправить подарок,\nпришли свой Telegram @username одним сообщением, обязательно с @ ♡\n\n"
                f"например: @username\n\n{FRAME}"
            )
    return render(order, username_field_id=configured[0]["username_field_id"] if configured else None)
