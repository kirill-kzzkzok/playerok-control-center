"""Playerok gift follow-ups reuse Executor's permit, lease and transport guards."""

import json

from app.database.store import now
from app.playerok.adapter import APIError
from app.services.order_management import deliverable, sale
from app.services.orders import Orders
from app.services.products import ValidationError
from playerokapi.enums import ItemDealStatuses

MESSAGES = {
    "DELIVERED": "Telegram-подарок отправлен на указанный при оплате аккаунт. Доставка подтверждена Telegram.",
    "REFUNDED": "Заказ отменён: указанный Telegram username не существует или некорректен. Возврат подтверждён Playerok.",
    "INVALID": "Возникла проблема с указанным Telegram username. Заказ передан продавцу для решения. Завершённый возврат пока не подтверждён.",
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
        if (
            job["state"] != "INVALID_RECIPIENT"
            or job["spent_at"]
            or job["form_id"]
            or job["refund_state"] != "SPENDING"
            or job["refund_operation_id"] != action["id"]
            or not service.runtime()
            or not service.settings.refund_enabled
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
    elif not permitted_notice(job, action["payload"].get("outcome")):
        raise ValidationError("Текст уведомления больше не соответствует результату операции")


async def execute_followup(executor, action):
    service = executor.gifts()
    target = action["target"]
    j = service.job(target)
    order = await executor.api.call("get_deal", deal_id=target)
    sale(order, target, executor.api.account.id)
    if (
        ((order.get("item") or {}).get("user") or {}).get("id") != executor.api.account.id
        or (order.get("item") or {}).get("id") != j["item_id"]
        or not (order.get("chat") or {}).get("id")
    ):
        raise ValidationError("Владелец/товар/чат сделки не подтверждены")
    Orders(executor.store).ingest(order)
    if action["kind"] == "gift_refund":
        deliverable(order, target, executor.api.account.id)
        capability = getattr(executor.api, "gift_refund_capability", None)
        if (
            not capability
            or not service.settings.refund_enabled
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
        return {"remote_id": target, "remote_status": "ROLLED_BACK"}
    outcome = action["payload"].get("outcome")
    if not permitted_notice(j, outcome):
        raise ValidationError("Результат выдачи/возврата не подтверждён")
    chat_id = order["chat"]["id"]
    chat = await executor.api.call("get_chat", chat_id=chat_id)
    if chat.get("id") != chat_id or chat.get("is_texting_allowed") is not True:
        raise ValidationError("Отправка уведомления в чат недоступна")
    result = await executor.mutate(
        action, "createChatMessage", "send_message", chat_id=chat_id, text=MESSAGES[outcome]
    )
    if not result.get("id"):
        raise APIError("GIFT_NOTICE_UNKNOWN", uncertain=True)
    return {"message_id": result["id"], "outcome": outcome}
