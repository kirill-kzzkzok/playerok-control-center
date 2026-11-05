"""Presentation-only vocabulary. Internal API and database values remain unchanged."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.telegram.formatting import escaped


def edit_readback_note(proof):
    """Explain failed server evidence without exposing submitted values."""
    if not isinstance(proof, dict) or not isinstance(proof.get("last_readback"), dict):
        return ""
    labels = {
        "id": "ID объявления",
        "owner": "владелец объявления",
        "name": "название",
        "description": "описание",
        "price": "цена",
        "attachments": "состав фотографий",
        "category": "категория",
        "obtaining_type": "способ получения",
        "attributes": "характеристики",
        "data_fields": "поля товара",
        "comment": "комментарий товара",
        "network": "ответ Playerok недоступен",
        "response": "неполный ответ Playerok",
    }
    fields = proof["last_readback"].get("fields")
    if not isinstance(fields, list):
        return ""
    names = [labels[field] for field in fields if isinstance(field, str) and field in labels]
    if not names:
        return ""
    return (
        "\nПоследняя проверка не подтвердила: "
        + ", ".join(names)
        + ". Значения отсутствуют в ответе или отличаются. Запрос изменения повторно не отправлялся."
    )


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
    "SENT": "Продавец отметил отправку",
    "CONFIRMED": "Получение подтверждено",
    "CONFIRMED_AUTOMATICALLY": "Завершён площадкой",
    "ROLLED_BACK": "Возврат подтверждён",
    "PENDING": "Ожидает оплаты",
}
ITEM = {
    "APPROVED": "Опубликован",
    "DISCONTINUED": "Снят с продажи",
    "DRAFT": "Черновик",
    "SOLD": "Продан",
    "EXPIRED": "Срок истёк",
    "PENDING_APPROVAL": "На проверке",
    "PENDING_MODERATION": "На модерации",
    "DELETED": "Удалён",
    "BLOCKED": "Заблокирован",
}
MODES = {"manual": "Ручная", "stock": "Со склада", "text": "Постоянный текст", "playerok": "Выдаёт Playerok"}


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
    if value.get("status") == "CONNECTED" and getattr(api.account, "connection_verified", False):
        if recent(value.get("last_success")):
            return "🟢 Playerok подключён"
        return "⚪ Playerok: аккаунт подтверждён\nПоследняя проверка: " + local_time(
            value.get("last_success")
        )
    return "🟡 Подключение Playerok нужно проверить"


def automation_summary(store, api, ident, *, item=None, compact=False):
    from app.services.product_readiness import ProductReadiness
    from app.telegram.product_presentation import summary

    return summary(ProductReadiness(store, api).snapshot(ident, item=item), compact=compact)


def error_text(error):
    from app.gifts.core import gift_reason
    from app.gifts.mtproto import GiftFailure
    from app.playerok.adapter import APIError
    from app.services.playerok_connection import REASONS

    if isinstance(error, GiftFailure):
        reasons = {
            "SESSION_UNAUTHORIZED": "Личный Telegram не авторизован. Выполните локальную авторизацию и проверьте подключение заново.",
            "SESSION_NOT_CONFIGURED": "Личный Telegram ещё не настроен. Выполните локальную авторизацию аккаунта.",
            "SESSION_CLOSED": "Соединение личного Telegram закрыто. Проверьте подключение в настройках подарков.",
            "PERSONAL_ACCOUNT_CHANGED": "Подключён другой личный Telegram-аккаунт. Покупки остановлены; проверьте исходную сессию.",
            "PERSONAL_ACCOUNT_REQUIRED": "Нужен личный Telegram-аккаунт пользователя. Проверьте выбранную сессию.",
            "CATALOG_UNAVAILABLE": "Telegram не подтвердил каталог подарков. Повторите проверку подключения без покупки.",
            "BALANCE_UNAVAILABLE": "Telegram не подтвердил баланс Stars. Повторите проверку подключения без покупки.",
            "GIFT_PAYMENTS_DISABLED": "Покупки подарков выключены в настройках подключения.",
            "DELIVERY_NOT_CONFIRMED": "Доставка не подтверждена. Откройте сверку операции; повторная покупка заблокирована.",
            "PAYMENT_NOT_CONFIRMED": "Результат платежа не подтверждён. Нужна сверка, повтор запрещён.",
            "PROXY_ROUTE_CHANGED": "Маршрут Telegram изменился. Нужна проверка операции; повторная покупка запрещена.",
        }
        return escaped(reasons.get(error.code, gift_reason(error.code)), 800)
    if isinstance(error, APIError):
        if error.code == "CHAT_ACCESS_FORBIDDEN":
            return "Playerok запретил доступ к этой переписке. Аккаунт проверен; остальные продажи продолжаются. Откройте диалог на Playerok и проверьте доступ."
        if error.code == "WRITES_DISABLED":
            return "Записи Playerok выключены или аккаунт не подтверждён. Проверьте разрешение записей и подключение в профиле."
        if error.code == "OPERATION_FORBIDDEN":
            return "Этот тип операции не поддерживается защищённым клиентом. Другие разрешённые функции доступны отдельно."
        reason = REASONS.get(error.code)
        if reason:
            return escaped(reason + ". Откройте профиль и проверьте соединение.", 800)
        return "Playerok не подтвердил действие. Проверьте результат в истории операций перед новым запросом."
    return escaped(str(error), 800)


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


MOSCOW = ZoneInfo("Europe/Moscow")


def local_time(value, *, clock=False):
    """Only timezone-aware source times are evidence of a moment."""
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            return "дата не указана"
        return moment.astimezone(MOSCOW).strftime("%H:%M" if clock else "%d.%m.%Y в %H:%M")
    except (ValueError, TypeError, AttributeError):
        return "дата не указана"


def short_title(value, limit=60, fallback="Без названия"):
    text = " ".join(str(value or "").split()) or fallback
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def chat_peer(value, account_id=None):
    """Chat.users is the API's participant collection; never guess from a UUID."""
    users = value.get("users") or []
    peers = [user for user in users if isinstance(user, dict) and user.get("id") != account_id]
    if len(peers) == 1:
        peer = peers[0]
        return peer.get("username") or peer.get("name") or "Собеседник не указан"
    user = value.get("user") or {}
    if not peers and isinstance(user, dict):
        return user.get("username") or user.get("name") or "Собеседник не указан"
    return "Собеседник не указан"


def history_event(event):
    source = event.get("source")
    if source == "LOCAL_NOTE":
        return "📝 Заметка продавца"
    if source == "DELIVERY_RESERVED":
        return "📦 Товар подготовлен к выдаче"
    if source == "API_STATE":
        names = {
            "PAID": "💳 Оплата подтверждена",
            "SENT": "📦 Продавец отметил отправку",
            "CONFIRMED": "✅ Покупатель подтвердил получение",
            "CONFIRMED_AUTOMATICALLY": "✅ Заказ завершён площадкой",
            "ROLLED_BACK": "↩️ Возврат подтверждён",
            "PENDING": "🛒 Заказ ожидает оплаты",
            "FAILED": "⚠️ Заказ не выполнен",
        }
        return names.get(event.get("new_status"), "Обновлён статус заказа")
    return "Обновлены данные заказа"


OPERATIONS = {
    "create": "Создание товара",
    "edit": "Изменение товара",
    "edit_fields": "Характеристики товара",
    "discount": "Цена и скидка",
    "delete": "Удаление товара",
    "publish": "Публикация",
    "premium_publish": "Премиум-публикация",
    "premium_bump": "Платное поднятие",
    "stock_pause": "Снятие с продажи: склад пуст",
    "stock_resume": "Возврат в продажу после пополнения",
    "gift_send": "Отправка подарка",
    "gift_refund": "Возврат заказа",
    "gift_notice": "Уведомление о подарке",
    "delivery": "Выдача со склада",
    "delivery_text": "Выдача текста",
    "chain_message": "Сообщение по заказу",
    "reply": "Ответ покупателю",
    "mark_read": "Отметка прочтения",
    "fulfill": "Подтверждение выполнения",
    "agreements": "Условия продавца",
}


def operation_name(value):
    return OPERATIONS.get(value, "Действие магазина")
