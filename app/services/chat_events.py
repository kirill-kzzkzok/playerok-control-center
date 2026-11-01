"""Known server events require an absent human author, never text alone."""

EVENTS = {
    "{{ITEM_PAID}}": "Оплата заказа подтверждена",
    "{{ITEM_SENT}}": "Продавец отметил выполнение заказа",
    "{{DEAL_CONFIRMED}}": "Получение заказа подтверждено",
    "{{DEAL_CONFIRMED_AUTOMATICALLY}}": "Заказ завершён площадкой",
    "{{DEAL_ROLLED_BACK}}": "Возврат по заказу подтверждён",
    "{{DEAL_HAS_PROBLEM}}": "В заказе возникла проблема — проверьте сделку",
    "{{DEAL_PROBLEM_RESOLVED}}": "Проблема заказа разрешена",
}


def system_event(message):
    return EVENTS.get(message.get("text")) if message.get("user") is None else None
