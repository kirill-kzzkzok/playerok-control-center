"""Strict public title suffix and durable, never-reused logical identity codes."""

import re
import secrets
import sqlite3
import string

from app.database.store import Conflict, now
from app.services.products_errors import ValidationError

ALPHABET = string.ascii_uppercase + string.digits
ATTEMPTS = 64
SUFFIX = re.compile(r" \[([A-Za-z0-9]{3})\]$")
CANDIDATE = re.compile(r"\[([A-Za-z0-9]{3})\]")


def normalized(code):
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]{3}", code):
        raise ValidationError("Код товара должен состоять из трёх латинских букв или цифр")
    return code.upper()


def title_code(name):
    if not isinstance(name, str):
        raise ValidationError("Playerok не вернул полное название товара")
    candidates = list(CANDIDATE.finditer(name))
    suffix = SUFFIX.search(name)
    if suffix and len(candidates) == 1 and name[: suffix.start()].strip():
        return normalized(suffix.group(1))
    if candidates or re.search(r"\[[^\[\]]*\]$", name) or name.endswith("]") or re.search(r"\[[^\]]*$", name):
        raise ValidationError("В названии нужен один код [XXX] строго в конце после названия")
    return None


def titled(name, code):
    code = normalized(code)
    if not isinstance(name, str):
        raise ValidationError("Введите название товара")
    existing = title_code(name)
    if existing and existing != code:
        raise Conflict("В названии указан код другого товара")
    human = name[: SUFFIX.search(name).start()] if existing else name
    value = human + " [" + code + "]"
    # Same Playerok UTF-16 contract as listing_text, without silently truncating.
    if not human.strip() or len(value.encode("utf-16-le")) // 2 > 72:
        raise ValidationError("Название вместе с кодом превышает 72 символа. Сократите название")
    return value


def choose_code(db):
    for _ in range(ATTEMPTS):
        code = "".join(secrets.choice(ALPHABET) for _ in range(3))
        if not db.execute("SELECT 1 FROM logical_code_reservations WHERE code=?", (code,)).fetchone():
            return code
    raise Conflict("Свободный код не найден. Пространство кодов занято; требуется проверка")


def reserve_code(db, product_id, preferred=None):
    if not isinstance(product_id, str) or not product_id:
        raise ValidationError("Не подтверждён логический товар")
    old = db.execute(
        "SELECT code FROM logical_code_reservations WHERE product_id=?", (product_id,)
    ).fetchone()
    if old:
        if preferred is not None and normalized(preferred) != old[0]:
            raise Conflict("Постоянный код товара уже закреплён и не может быть заменён")
        return old[0]
    for _ in range(ATTEMPTS):
        code = normalized(preferred) if preferred is not None else choose_code(db)
        try:
            db.execute(
                "INSERT INTO logical_code_reservations(code,product_id,created_at) VALUES(?,?,?)",
                (code, product_id, now()),
            )
            return code
        except sqlite3.IntegrityError:
            if not db.execute("SELECT 1 FROM logical_code_reservations WHERE code=?", (code,)).fetchone():
                raise
            if preferred is not None:
                raise Conflict("Код из превью уже занят. Обновите план и подтвердите новый код") from None
    raise Conflict("Не удалось закрепить свободный код")


def remove_legacy_tail(description, old_code):
    if not isinstance(description, str) or not re.fullmatch(r"#[0-9]{7}", old_code or ""):
        raise ValidationError("Не подтверждены исходное описание и архивный код")
    lines = description.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.strip() == old_code]
    if not matches:
        return description
    last = max((i for i, line in enumerate(lines) if line.strip()), default=-1)
    if len(matches) != 1 or matches[0] != last:
        raise Conflict("Архивный код расположен неоднозначно. Проверьте описание вручную")
    return "".join(line for i, line in enumerate(lines) if i != matches[0])
