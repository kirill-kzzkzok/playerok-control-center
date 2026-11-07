import re
from uuid import uuid4

from app.database.store import now
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
        "archive",
        "delivery",
        "problems",
    }:
        raise ValidationError("Некорректные фильтры товаров")
    status = value.get("status")
    if any(type(value.get(key, False)) is not bool for key in ("delivery", "problems")):
        raise ValidationError("Некорректный фильтр выдачи или проблем")
    if not isinstance(value.get("archive", False), bool):
        raise ValidationError("Некорректный режим архива")
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
    if filters.get("archive") and item.get("status") in {None, "APPROVED", "DRAFT"}:
        return False
    if filters.get("status") and item.get("status") != filters["status"]:
        return False
    query = filters.get("search", "").casefold()
    if (
        query not in str(item.get("name") or "").casefold()
        and query != str(item.get("search_code") or "").casefold()
    ):
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


def catalog_item(value, account):
    from app.playerok.adapter import APIError
    from app.services.logical_products import marker

    seller = (value.get("user") or {}).get("id")
    if seller and seller != account:
        raise APIError("CATALOG_ACCOUNT_MISMATCH")
    item = {key: value.get(key) for key in ("id", "name", "price", "status", "priority", "deleted_at")}
    item["category"] = {"id": (value.get("category") or {}).get("id")}
    try:
        item["search_code"] = marker(value.get("name") or "")
    except ValueError:
        item["search_code"] = None
    return item


async def catalog_page(api, account, cache):
    from app.playerok.adapter import APIError, validate_page

    page = await api.call("get_my_items", count=24, after_cursor=cache.get("cursor"), statuses=None)
    if api.account.id != account:
        raise APIError("CATALOG_ACCOUNT_MISMATCH")
    seen = cache.get("seen", [])
    values, cursor = validate_page(page, "items", seen)
    items = {item["id"]: item for item in cache.get("items", [])}
    items.update((value["id"], catalog_item(value, account)) for value in values)
    return {
        "at": cache.get("at") or now(),
        "items": list(items.values()),
        "cursor": cursor,
        "seen": [*seen, cursor] if cursor else [],
        "complete": not cursor,
    }


async def refresh_due_catalog(store, api):
    import asyncio
    import time

    from app.playerok.adapter import APIError

    account = api.account.id
    if not account or account != store.setting("playerok:account_id"):
        return
    key = "ui:catalog:" + account
    pending_key = key + ":pending"
    request = store.setting(pending_key)
    if not request or request.get("due", 0) > time.time():
        return
    try:
        async with asyncio.timeout(8):
            cache = await catalog_page(api, account, request.get("scan", {}))
        if account != store.setting("playerok:account_id"):
            return
    except (APIError, TimeoutError, ValueError, TypeError, KeyError) as error:
        failures = min(request.get("failures", 0) + 1, 5)
        retry_after = getattr(error, "retry_after", None)
        delay = max(30 * 2**failures, int(retry_after) if str(retry_after).isdigit() else 0)
        store.set_setting(pending_key, {**request, "failures": failures, "due": time.time() + delay})
    else:
        with store.transaction():
            count = (
                sum(
                    item.get("status") == "APPROVED" and not item.get("deleted_at") for item in cache["items"]
                )
                if cache["complete"]
                else None
            )
            store.set_setting("shop:active:" + account, {"count": count, "at": now()})
            if cache["complete"]:
                previous = store.setting(key, {})
                if previous.get("at"):
                    store.set_setting(key + ":previous", previous)
                store.set_setting(key, {**cache, "background": True, "generation": uuid4().hex})
                store.execute("DELETE FROM settings WHERE key=?", (pending_key,))
            else:
                store.set_setting(pending_key, {"scan": cache, "due": 0})
