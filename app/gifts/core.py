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

TAG = re.compile(r"^gift:\s*([a-z][a-z0-9_-]{0,30})\s*$", re.IGNORECASE)
USERNAME = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")


class GiftValidationError(ValueError):
    pass


def gift_code_from_description(description: str, *, required: bool = True) -> str | None:
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
    code = matches[0][1].group(1).lower()
    if code not in SEED_GIFTS:
        raise GiftValidationError("Неизвестный код подарка. Доступные: " + ", ".join(SEED_GIFTS))
    return code


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
