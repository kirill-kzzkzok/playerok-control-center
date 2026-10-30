import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.services.finance import money

PAID = {"PAID", "SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"}
CONFIRMED = {"CONFIRMED", "CONFIRMED_AUTOMATICALLY"}


def parsed_time(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def amount_or_none(value):
    try:
        return money(value) if value is not None else None
    except ValueError:
        return None


def statistics(store, days=7, at=None):
    if days not in {1, 2, 7, 30, 0}:
        raise ValueError("Недопустимый период")
    at = at or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("Требуется часовой пояс")
    start = at - timedelta(days=days) if days else datetime.min.replace(tzinfo=UTC)
    previous_start = start - timedelta(days=days) if days else start
    result = {
        "orders": 0,
        "paid": 0,
        "confirmed": 0,
        "refunded": 0,
        "revenue": Decimal(0),
        "confirmed_revenue": Decimal(0),
        "refunds": Decimal(0),
        "previous_revenue": Decimal(0),
        "previous_paid": 0,
        "previous_unknown_paid": 0,
        "daily": defaultdict(lambda: Decimal(0)),
        "categories": defaultdict(lambda: Decimal(0)),
        "items": defaultdict(lambda: Decimal(0)),
        "item_names": {},
        "item_orders": defaultdict(int),
        "item_unknown_paid": defaultdict(int),
        "category_orders": defaultdict(int),
        "category_unknown_paid": defaultdict(int),
        "daily_orders": defaultdict(int),
        "daily_paid": defaultdict(int),
        "daily_unknown_paid": defaultdict(int),
        "hourly": defaultdict(lambda: Decimal(0)),
        "hourly_orders": defaultdict(int),
        "unknown_amounts": 0,
        "unknown_paid_amounts": 0,
        "unknown_dates": 0,
        "coverage_start": None,
        "coverage_end": None,
        "last_sync": store.setting("orders:last_complete_sync"),
        "expenses": Decimal(0),
        "promotion_expenses": Decimal(0),
        "promotion_daily": defaultdict(lambda: Decimal(0)),
        "unknown_expenses": 0,
        "period_start": start.isoformat(),
        "period_end": at.isoformat(),
        "days": days,
        "basis": "created_order_cohort_current_status",
    }
    coverage = []
    name_dates = {}
    for order in store.rows("SELECT * FROM orders"):
        payload = json.loads(order["payload"])
        # Missing upstream time cannot become a real historical date via ingest fallback.
        created = parsed_time(payload.get("created_at"))
        if created is None:
            result["unknown_dates"] += 1
            continue
        if created <= at:
            coverage.append(created)
        if not previous_start <= created <= at:
            continue
        amount = amount_or_none(order["amount"])
        if created < start:
            if order["status"] in PAID:
                result["previous_paid"] += 1
                if amount is not None:
                    result["previous_revenue"] += amount
                else:
                    result["previous_unknown_paid"] += 1
            continue
        result["orders"] += 1
        local = created.astimezone(ZoneInfo("Europe/Moscow"))
        day, hour = local.strftime("%Y-%m-%d"), local.strftime("%Y-%m-%d %H:00")
        result["daily_orders"][day] += 1
        result["hourly_orders"][hour] += 1
        if amount is None:
            result["unknown_amounts"] += 1
        if order["status"] in PAID:
            result["paid"] += 1
            result["daily_paid"][day] += 1
            category = ((payload.get("item") or {}).get("category") or {}).get("name") or "Без категории"
            result["category_orders"][category] += 1
            item_id = order["item_id"]
            result["item_orders"][item_id] += 1
            name_order = (created, order["id"])
            if item_id not in name_dates or name_order > name_dates[item_id]:
                name_dates[item_id] = name_order
                result["item_names"][item_id] = (payload.get("item") or {}).get("name") or item_id
            if amount is None:
                result["unknown_paid_amounts"] += 1
                result["daily_unknown_paid"][day] += 1
                result["item_unknown_paid"][item_id] += 1
                result["category_unknown_paid"][category] += 1
            else:
                result["revenue"] += amount
                result["daily"][day] += amount
                result["hourly"][hour] += amount
                result["categories"][category] += amount
                result["items"][order["item_id"]] += amount
            if order["status"] in CONFIRMED:
                result["confirmed"] += 1
                if amount is not None:
                    result["confirmed_revenue"] += amount
        elif order["status"] == "ROLLED_BACK":
            result["refunded"] += 1
            if amount is not None:
                result["refunds"] += amount
    if coverage:
        result.update(coverage_start=min(coverage).isoformat(), coverage_end=max(coverage).isoformat())
    for expense in store.rows(
        "SELECT amount,currency,created_at FROM financial_operations WHERE kind='EXPENSE'"
    ):
        when = parsed_time(expense["created_at"])
        amount = amount_or_none(expense["amount"])
        if when is None or amount is None or expense["currency"] != "RUB":
            result["unknown_expenses"] += 1
        elif start <= when <= at:
            result["expenses"] += amount
    for expense in store.rows("SELECT amount,created_at FROM promotion_expenses WHERE status='SUCCEEDED'"):
        when = parsed_time(expense["created_at"])
        amount = amount_or_none(expense["amount"])
        if when is None or amount is None:
            result["unknown_expenses"] += 1
        elif start <= when <= at:
            result["promotion_expenses"] += amount
            result["promotion_daily"][when.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d")] += (
                amount
            )
    result["average"] = (
        (result["revenue"] / result["paid"]).quantize(Decimal("0.01"))
        if result["paid"] and not result["unknown_paid_amounts"]
        else None
    )
    result["daily_average"] = {
        day: (result["daily"][day] / count).quantize(Decimal("0.01"))
        for day, count in result["daily_paid"].items()
        if count and not result["daily_unknown_paid"][day]
    }
    result["previous_average"] = (
        (result["previous_revenue"] / result["previous_paid"]).quantize(Decimal("0.01"))
        if result["previous_paid"] and not result["previous_unknown_paid"]
        else None
    )
    result["growth_percent"] = (
        ((result["revenue"] - result["previous_revenue"]) / result["previous_revenue"] * 100).quantize(
            Decimal("0.01")
        )
        if days
        and result["previous_revenue"]
        and not result["unknown_paid_amounts"]
        and not result["previous_unknown_paid"]
        else None
    )
    return result
