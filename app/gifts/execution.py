"""Playerok gift follow-ups reuse Executor's permit, lease and transport guards."""

import json

from app.database.store import now
from app.gifts.core import buyer_recipient
from app.playerok.adapter import APIError
from app.security.logging import event
from app.services.order_management import deliverable, sale
from app.services.orders import Orders
from app.services.paid_greeting import FRAME
from app.services.products import ValidationError
from playerokapi.enums import ItemDealStatuses

MESSAGES = {
    "DELIVERED": (
        f"{FRAME}\n\n🎀 ура, твой заказ доставлен! ♡\n\n"
        "🎁 все подарки успешно отправлены\nна указанный Telegram-аккаунт.\n\n"
        f"💗 спасибо, что выбрал мой магазин!\nбуду очень рада видеть тебя снова ♡\n\n{FRAME}"
    ),
    "REFUNDED": (
        f"{FRAME}\n\n💌 ой, кажется, что-то пошло не так...\n\n"
        "не получилось найти Telegram-аккаунт\nпо указанному username 🥺\n\n"
        f"💗 заказ отменён, а возврат\nсредств уже подтверждён.\n\nнадеюсь, увидимся снова! ♡\n\n{FRAME}"
    ),
    "INVALID": (
        f"{FRAME}\n\n💌 возникла небольшая проблема...\n\n"
        "не получилось отправить подарок\nна указанный Telegram-аккаунт.\n\n"
        "🧸 заказ передан продавцу,\nчтобы разобраться в ситуации.\n\n"
        f"💗 возврат средств пока\nне подтверждён. ♡\n\n{FRAME}"
    ),
}


def permitted_notice(job, outcome):
    return (
        (outcome == "DELIVERED" and job["state"] == "SUCCEEDED" and bool(job["receipt"]))
        or (outcome == "REFUNDED" and job["refund_state"] == "CONFIRMED" and not job["spent_at"])
        or (
            outcome == "INVALID"
            and job["state"] == "INVALID_RECIPIENT"
            and job["refund_state"] in {"MANUAL", "FAILED", "UNCERTAIN"}
            and not job["spent_at"]
        )
    )


def guard(service, action):
    job = service.job(action["target"])
    if action["kind"] == "gift_refund":
        from app.services.logical_products import LogicalProducts, refund_allowed

        LogicalProducts(service.store, service.api).require_delivery(
            job["item_id"], "telegram_gifts", job["order_id"]
        )
        if (
            job["state"] != "INVALID_RECIPIENT"
            or job["spent_at"]
            or job["form_id"]
            or job["refund_state"] != "SPENDING"
            or job["refund_operation_id"] != action["id"]
            or not service.runtime()
            or not service.settings.refund_enabled
            or not refund_allowed(service.store, job["item_id"], job["order_id"])
        ):
            raise ValidationError("Возврат подарочного заказа заблокирован")
        local = service.store.rows("SELECT status,payload FROM orders WHERE id=?", (job["order_id"],))
        if (
            not local
            or local[0]["status"] != "PAID"
            or service.store.rows(
                "SELECT 1 FROM order_annotations WHERE order_id=? AND problem=1", (job["order_id"],)
            )
        ):
            raise ValidationError("Сохранённая история больше не разрешает возврат")
        deliverable(json.loads(local[0]["payload"]), job["order_id"], service.api.account.id)
        if (
            buyer_recipient(
                json.loads(local[0]["payload"]), field_id=service.listing(job["item_id"])["username_field_id"]
            ).casefold()
            != (job["recipient_username"] or "").casefold()
        ):
            raise ValidationError("Данные получения изменились; возврат требует ручного решения")
    elif not permitted_notice(job, action["payload"].get("outcome")):
        raise ValidationError("Текст уведомления больше не соответствует результату операции")


async def execute_followup(executor, action):
    service = executor.gifts()
    target = action["target"]
    j = service.job(target)
    from app.services.order_management import deal_details

    order = await deal_details(executor.api, target)
    sale(order, target, executor.api.account.id)
    if (
        ((order.get("item") or {}).get("user") or {}).get("id") != executor.api.account.id
        or (order.get("item") or {}).get("id") != j["item_id"]
        or not (order.get("chat") or {}).get("id")
    ):
        raise ValidationError("Владелец/товар/чат сделки не подтверждены")
    Orders(executor.store).ingest(order)
    if action["kind"] == "gift_refund":
        from app.services.logical_products import refund_allowed

        deliverable(order, target, executor.api.account.id)
        if (
            buyer_recipient(order, field_id=service.listing(j["item_id"])["username_field_id"]).casefold()
            != (j["recipient_username"] or "").casefold()
        ):
            raise ValidationError("Данные получения изменились; возврат требует ручного решения")
        capability = getattr(executor.api, "gift_refund_capability", None)
        if (
            not capability
            or not service.settings.refund_enabled
            or not refund_allowed(executor.store, j["item_id"], j["order_id"])
            or await capability(order) is not True
            or not service.runtime()
            or j["state"] != "INVALID_RECIPIENT"
            or j["spent_at"]
            or j["form_id"]
            or j["refund_operation_id"] != action["id"]
            or j["refund_state"] != "QUEUED"
        ):
            raise ValidationError("Допустимость возврата не подтверждена")
        if (
            executor.store.execute(
                "UPDATE gift_jobs SET refund_state='SPENDING',updated_at=? WHERE order_id=? AND refund_state='QUEUED' AND spent_at IS NULL",
                (now(), target),
            ).rowcount
            != 1
        ):
            raise ValidationError("Возврат уже начат")
        await executor.mutate(
            action, "updateDeal", "update_deal", deal_id=target, new_status=ItemDealStatuses.ROLLED_BACK
        )
        confirmed = await executor.api.call("get_deal", deal_id=target)
        sale(confirmed, target, executor.api.account.id)
        if (
            confirmed.get("status") != "ROLLED_BACK"
            or (confirmed.get("item") or {}).get("id") != j["item_id"]
            or ((confirmed.get("item") or {}).get("user") or {}).get("id") != executor.api.account.id
        ):
            raise APIError("REFUND_NOT_CONFIRMED", uncertain=True)
        Orders(executor.store).ingest(confirmed)
        event("CHECK", "Возврат подтверждён Playerok", order=target, operation=action["id"])
        return {"remote_id": target, "remote_status": "ROLLED_BACK"}
    outcome = action["payload"].get("outcome")
    if not permitted_notice(j, outcome):
        raise ValidationError("Результат выдачи/возврата не подтверждён")
    chat_id = order["chat"]["id"]
    chat = await executor.api.call("get_chat", chat_id=chat_id)
    if chat.get("id") != chat_id or chat.get("is_texting_allowed") is not True:
        raise ValidationError("Отправка уведомления в чат недоступна")
    text = MESSAGES[outcome]
    if outcome == "DELIVERED":
        after = executor.store.setting("gifts:listing_options:" + j["item_id"], {}).get("after", "")
        scopes = executor.store.rows("SELECT config FROM logical_order_scopes WHERE order_id=?", (target,))
        if scopes:
            after = json.loads(scopes[0]["config"]).get("after", "")
        if after:
            from app.services.automation import message_text

            text = message_text(after, required=True)
    result = await executor.mutate(action, "createChatMessage", "send_message", chat_id=chat_id, text=text)
    if not result.get("id"):
        raise APIError("GIFT_NOTICE_UNKNOWN", uncertain=True)
    event("CHECK", "Уведомление покупателю отправлено", order=target, operation=action["id"])
    return {"message_id": result["id"], "outcome": outcome}
