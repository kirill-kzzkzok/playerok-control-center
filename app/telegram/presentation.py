"""Presentation-only vocabulary. Internal API and database values remain unchanged."""

from datetime import UTC, datetime

from app.telegram.formatting import escaped

COMMON = {
    "QUEUED": "Ожидает выполнения",
    "PENDING": "Ожидает",
    "PROCESSING": "Выполняется",
    "SUCCEEDED": "Выполнено и подтверждено",
    "UNCERTAIN": "Результат неизвестен — нужна проверка",
    "UNKNOWN": "Не удалось установить результат",
    "PERMANENT_FAILED": "Действие не выполнено",
    "RETRYABLE_FAILED": "Ожидает безопасной обработки",
    "FAILED": "Не выполнено",
    "BLOCKED": "Остановлено — требуется решение",
    "SPENDING": "Платёж начат",
    "PREPARING": "Подготовка",
    "CHECKING": "Проверяем условия",
    "READY": "Готово",
    "STOPPED": "Остановлено",
    "CONFIRMED": "Подтверждено",
    "MANUAL": "Требуется решение продавца",
    "NONE": "Нет",
    "NOT_STARTED": "Ещё не начато",
    "CHECK_FAILED": "Проверка не удалась",
    "BACKFILL": "Загружаем историю",
    "ERROR": "Ошибка проверки",
    "OK": "Проверено",
    "INVALID_RECIPIENT": "Получатель не подтверждён",
    "PARTIALLY_DELIVERED": "Доставлено частично",
}
ORDER = {
    **COMMON,
    "PAID": "Оплачен",
    "SENT": "Продавец отметил выполнение",
    "CONFIRMED": "Получение подтверждено",
    "CONFIRMED_AUTOMATICALLY": "Завершён площадкой",
    "ROLLED_BACK": "Возврат подтверждён",
    "PENDING": "Ожидает оплаты",
}
ITEM = {
    "APPROVED": "Опубликован",
    "DRAFT": "Черновик",
    "SOLD": "Продан",
    "EXPIRED": "Срок истёк",
    "PENDING_APPROVAL": "На проверке",
    "PENDING_MODERATION": "На модерации",
    "DELETED": "Удалён",
    "BLOCKED": "Заблокирован",
}
MODES = {"manual": "Ручная", "stock": "Со склада", "text": "Постоянный текст"}


def status(value, context="operation"):
    mapping = ORDER if context == "order" else ITEM if context == "item" else COMMON
    # Unknown server states must not be guessed or presented as success.
    return mapping.get(value, "Статус не распознан — откройте подробности" if value else "Нет данных")


def recent(value, seconds=300):
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            return False
        age = (datetime.now(UTC) - moment).total_seconds()
        return 0 <= age <= seconds
    except (TypeError, ValueError, AttributeError):
        return False


def connection_summary(api):
    manager = getattr(api, "connection", None)
    value = getattr(manager, "state", {})
    if value.get("status") == "DISCONNECTED":
        return "🔴 Playerok не подключён"
    if (
        value.get("status") == "CONNECTED"
        and recent(value.get("last_success"))
        and getattr(api.account, "connection_verified", False)
    ):
        return "🟢 Playerok подключён"
    return "🟡 Подключение Playerok нужно проверить"


def automation_summary(store, api, ident):
    from app.services.automation import Automation

    value = Automation(store, api).readiness(ident)
    return f"Выдача: {value['status'].lower()} · {MODES[value['mode']]}" + (
        "\n" + escaped("; ".join(value["reasons"]), 650) if value["reasons"] else ""
    )


def error_text(error):
    from app.playerok.adapter import APIError
    from app.services.playerok_connection import REASONS

    if isinstance(error, APIError):
        reason = REASONS.get(error.code)
        if reason:
            return escaped(reason + ". Откройте профиль и проверьте соединение.", 800)
        return "Playerok не подтвердил действие. Проверьте результат в истории операций перед новым запросом."
    return escaped(str(error), 800)


def context_back(markup, action):
    """Replace only the generic home row; explicit back buttons and proofs stay intact."""
    from aiogram.types import InlineKeyboardButton

    if not markup:
        return markup
    target = None
    if action.startswith(("gft:",)) and action != "gft:home":
        target = ("⬅️ Подарки", "gft:home")
    elif action.startswith(("stock:",)):
        target = ("⬅️ К товарам", "items")
    elif action.startswith(("od:", "ff:", "pf:")):
        target = ("⬅️ К заказам", "orders")
    elif action.startswith(("chat", "reply:", "compose:", "readchat:")):
        target = ("⬅️ К сообщениям", "chats")
    elif action.startswith(("edit", "discount:", "delete:", "publish:", "quote:", "finance:")):
        ident = action.split(":", 1)[1]
        target = ("⬅️ К товару", "item:" + ident)
    elif action.startswith(("draft", "dl:", "il:", "itemfilter", "icp:")):
        target = ("⬅️ К товарам", "items")
    elif action.startswith(("sp:", "ar:", "quick", "providers", "fin:")):
        target = ("⬅️ Настройки", "settings")
    elif action.startswith(("ops:", "result:", "aq:", "delivered:", "agv:", "ic:")) or action in {
        "diagnostics",
        "sync",
        "backup",
    }:
        target = ("⬅️ Инструменты", "tools")
    elif action in {"settings", "profile", "profile:check"}:
        target = ("⬅️ Ещё", "more")
    if target:
        markup = markup.model_copy(deep=True)
        rows = markup.inline_keyboard
        has_target = any(b.callback_data == target[1] for row in rows for b in row)
        markup.inline_keyboard = [
            row for row in rows if not (len(row) == 1 and row[0].callback_data == "home")
        ]
        if not has_target:
            markup.inline_keyboard.append([InlineKeyboardButton(text=target[0], callback_data=target[1])])
    return markup


def item_label(store, ident):
    cached = store.setting("ui:item:" + ident + ":name")
    if isinstance(cached, str) and cached:
        return cached[:70]
    rows = store.rows(
        "SELECT json_extract(payload,'$.item.name') AS name FROM orders WHERE item_id=? AND json_extract(payload,'$.item.name') IS NOT NULL ORDER BY created_at DESC LIMIT 1",
        (ident,),
    )
    return str(rows[0]["name"])[:70] if rows else "Товар без сохранённого названия"


def order_label(store, ident):
    rows = store.rows("SELECT json_extract(payload,'$.item.name') AS name FROM orders WHERE id=?", (ident,))
    return str(rows[0]["name"] or "Заказ")[:60] if rows else "Заказ"
