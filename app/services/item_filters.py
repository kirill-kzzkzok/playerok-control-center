import re

from app.services.finance import money
from app.services.products import ValidationError
from playerokapi.enums import ItemStatuses


def checked_filters(value):
    if not isinstance(value, dict) or set(value) - {
        "status",
        "search",
        "minimum",
        "maximum",
        "category_id",
        "priority",
    }:
        raise ValidationError("Некорректные фильтры товаров")
    status = value.get("status")
    if status and status not in ItemStatuses.__members__:
        raise ValidationError("Неизвестный статус товара")
    search = value.get("search", "")
    if not isinstance(search, str) or len(search) > 200:
        raise ValidationError("Поиск: не более 200 символов")
    category = value.get("category_id")
    if category is not None and (
        not isinstance(category, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", category)
    ):
        raise ValidationError("Категория: ID из букв, цифр, дефиса или подчёркивания")
    if value.get("priority") not in {None, "PREMIUM", "DEFAULT"}:
        raise ValidationError("Неизвестный фильтр приоритета")
    result = dict(value)
    for key in ("minimum", "maximum"):
        if result.get(key) is not None:
            result[key] = str(money(result[key]))
    if (
        result.get("minimum") is not None
        and result.get("maximum") is not None
        and money(result["minimum"]) > money(result["maximum"])
    ):
        raise ValidationError("Минимальная цена больше максимальной")
    result["search"] = search.strip()
    return result


def matches_item(item, filters):
    if filters.get("search", "").casefold() not in str(item.get("name") or "").casefold():
        return False
    if filters.get("priority") and item.get("priority") != filters["priority"]:
        return False
    if filters.get("minimum") is not None or filters.get("maximum") is not None:
        try:
            price = money(item.get("price"))
        except ValueError:
            return False
        if filters.get("minimum") is not None and price < money(filters["minimum"]):
            return False
        if filters.get("maximum") is not None and price > money(filters["maximum"]):
            return False
    return True
