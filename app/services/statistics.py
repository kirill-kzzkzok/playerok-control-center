import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.finance import money


def statistics(store, days=7, at=None):
    if days not in {1, 2, 7, 30, 0}:
        raise ValueError("Недопустимый период")
    at = at or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("Требуется часовой пояс")
    start = at - timedelta(days=days) if days else datetime.min.replace(tzinfo=UTC)
    previous_start = start - timedelta(days=days) if days else start
    orders = store.rows("SELECT * FROM orders ORDER BY created_at")
    result = {
        "orders": 0,
        "paid": 0,
        "confirmed": 0,
        "refunded": 0,
        "revenue": Decimal(0),
        "confirmed_revenue": Decimal(0),
        "refunds": Decimal(0),
        "previous_revenue": Decimal(0),
        "daily": defaultdict(lambda: Decimal(0)),
        "categories": defaultdict(lambda: Decimal(0)),
        "items": defaultdict(lambda: Decimal(0)),
        "unknown_amounts": 0,
        "coverage_start": orders[0]["created_at"] if orders else None,
        "last_sync": store.setting("orders:last_complete_sync"),
        "expenses": Decimal(0),
    }
    paid = {"PAID", "SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"}
    from zoneinfo import ZoneInfo

    for order in orders:
        created = datetime.fromisoformat(order["created_at"])
        if created.tzinfo is None:
            continue
        if not previous_start <= created <= at:
            continue
        amount = money(order["amount"]) if order["amount"] is not None else None
        if created < start:
            if order["status"] in paid and amount is not None:
                result["previous_revenue"] += amount
            continue
        result["orders"] += 1
        if amount is None:
            result["unknown_amounts"] += 1
        if order["status"] in paid:
            result["paid"] += 1
            if amount is not None:
                result["revenue"] += amount
                result["daily"][created.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d")] += amount
                payload = json.loads(order["payload"])
                category = ((payload.get("item") or {}).get("category") or {}).get("name") or "Без категории"
                result["categories"][category] += amount
                result["items"][order["item_id"]] += amount
            if order["status"] in {"CONFIRMED", "CONFIRMED_AUTOMATICALLY"}:
                result["confirmed"] += 1
                if amount is not None:
                    result["confirmed_revenue"] += amount
        elif order["status"] == "ROLLED_BACK":
            result["refunded"] += 1
            if amount is not None:
                result["refunds"] += amount
    for expense in store.rows("SELECT amount,created_at FROM financial_operations WHERE kind='EXPENSE'"):
        when = datetime.fromisoformat(expense["created_at"])
        if start <= when <= at:
            result["expenses"] += money(expense["amount"])
    result["average"] = (
        (result["revenue"] / result["paid"]).quantize(Decimal("0.01"))
        if result["paid"] and not result["unknown_amounts"]
        else None
    )
    return result
