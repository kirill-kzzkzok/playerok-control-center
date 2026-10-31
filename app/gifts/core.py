"""Pure validation and inventory metadata for Telegram star gifts.

This module deliberately contains no Telegram token, credentials or network code.
"""

from __future__ import annotations

# User-supplied getAvailableGifts snapshot in versioned structured data.
# Every paid send MUST verify its gift ID and price against current Telegram MTProto.
import json
import re
from dataclasses import dataclass
from pathlib import Path

_catalog_file = Path(__file__).with_name("catalog.json")
_catalog = json.loads(_catalog_file.read_text(encoding="utf-8"))
SEED_GIFTS: dict[str, tuple[str, int, int]] = {
    row["code"]: (row["emoji"], int(row["gift_id"]), int(row["stars"])) for row in _catalog["gifts"]
}


def gift_name(code):
    """Display labels never participate in ID selection or payment intent."""
    return {"bear": "🧸 Медведь", "zombie_bear": "🧟 Зомби-медведь"}.get(
        code, f"{SEED_GIFTS[code][0]} {code}" if code in SEED_GIFTS else code
    )


TAG = re.compile(r"^gift:\s*(.+?)\s*$", re.IGNORECASE)
USERNAME = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")


class GiftValidationError(ValueError):
    pass


def gift_reason(code):
    return {
        "BALANCE_TOO_LOW": "Недостаточно Stars для оставшегося пакета",
        "BUDGET_EXCEEDED": "Суммарная стоимость превышает лимит заказа или суток",
        "PRICE_CHANGED": "Изменилась цена подарка: подтвердите новую привязку",
        "GIFT_UNAVAILABLE": "Подарок недоступен: обновите каталог и проверьте привязку",
        "GIFT_QUANTITY_UNAVAILABLE": "Недостаточно доступных подарков выбранного вида",
        "RECIPIENT_CHANGED": "Получатель изменился: отправка заблокирована",
        "RECIPIENT_GIFTS_FORBIDDEN": "Получатель запретил приём этого вида подарков",
        "RECIPIENT_NOT_USER": "Адресат не является поддерживаемым пользователем Telegram",
        "USERNAME_INVALID": "Telegram подтвердил некорректный username",
        "USERNAME_NOT_OCCUPIED": "Telegram подтвердил отсутствие username",
        "PAYMENT_LOCKED": "Другая подарочная оплата выполняется или требует сверки",
        "BALANCE_OBSERVATION_STALE": "Баланс проверен до другого списания: нужна новая безопасная проверка",
        "AUTOMATION_STOPPED_OR_STALE": "Выдача остановлена или политика изменилась",
        "LISTING_CHANGED": "Описание или способ передачи изменились",
        "GIFT_CHECK_FAILED": "Ошибка проверки Telegram до новой оплаты; проверьте подключение",
        "GIFT_PAYMENT_UNKNOWN": "Ответ после начала оплаты потерян: только сверка, без повтора",
        "PROCESS_CANCELLED": "Операция прервана: проверьте платёжный intent перед продолжением",
    }.get(code, "Условия операции не подтверждены")


def gift_code_from_description(description: str, *, required: bool = True, max_count: int = 5) -> str | None:
    """Only a unique gift: code on the final non-empty description line is authoritative."""
    if not isinstance(description, str):
        raise GiftValidationError("Описание должно быть текстом")
    lines = [s.strip() for s in description.splitlines() if s.strip()]
    tags = [(i, TAG.fullmatch(s)) for i, s in enumerate(lines)]
    matches = [(i, m) for i, m in tags if m]
    if sum(line.casefold().startswith("gift:") for line in lines) != len(matches):
        raise GiftValidationError("Некорректная или неоднозначная метка gift:")
    if not matches:
        if required:
            raise GiftValidationError(
                "Последняя строка описания должна быть gift: heart (или другой код подарка)"
            )
        return None
    if len(matches) != 1 or matches[0][0] != len(lines) - 1:
        raise GiftValidationError("Метка gift: должна быть одна и строго последней строкой")
    codes = [code.strip().lower() for code in matches[0][1].group(1).split(",")]
    if not 1 <= len(codes) <= max_count:
        raise GiftValidationError(f"Допустимо от 1 до {max_count} подарков")
    if any(code not in SEED_GIFTS for code in codes):
        raise GiftValidationError("Неизвестный код подарка. Доступные: " + ", ".join(SEED_GIFTS))
    return ", ".join(codes)


def gift_codes(value):
    return value.split(", ")


def telegram_username(value: str) -> str:
    """Normalize only a human username explicitly supplied in the buyer's obtaining field."""
    if not isinstance(value, str):
        raise GiftValidationError("Telegram username получателя отсутствует")
    candidate = value.strip()
    match = USERNAME.fullmatch(candidate)
    if not match:
        raise GiftValidationError("Нужен Telegram @username (5–32 символа, латиница/цифры/_)")
    return "@" + match.group(1)


def buyer_recipient(order: dict, *, field_id: str) -> str:
    """Never use order.user.username (that's a Playerok profile username).

    Playerok's buyer form is available as obtaining_fields; using the free-form
    comment or unrelated automation fields as a fallback is intentionally forbidden.
    """
    if not field_id:
        raise GiftValidationError("Не настроен точный ID поля получения Telegram username")
    fields = order.get("obtaining_fields")
    if not isinstance(fields, list):
        raise GiftValidationError("Playerok не вернул поля получения заказа")
    candidates: list[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        ident = str(field.get("id") or "")
        if ident == field_id:
            candidates.append(field.get("value"))
    if len(candidates) != 1:
        raise GiftValidationError("Нельзя однозначно найти Telegram username в поле покупки")
    return telegram_username(candidates[0])


@dataclass(frozen=True)
class GiftIntent:
    order_id: str
    item_id: str
    gift_code: str
    recipient: str
    gift_id: int
    stars: int
