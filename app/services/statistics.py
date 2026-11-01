import json
import math
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


def delivery_statistics(store, days=0):
    """Local observation durations, never created_at masquerading as paid_at."""
    series = {
        key: []
        for key in (
            "observed",
            "server_paid",
            "preparation",
            "confirmation",
            "deal_check",
            "recipient",
            "payment_form",
            "payment_rpc",
            "receipt_read",
        )
    }
    completed = 0
    cutoff = datetime.now(UTC) - timedelta(days=days) if days else None
    for job in store.rows(
        "SELECT g.*,i.received_at FROM gift_jobs g JOIN inbox i ON i.key='order:'||g.order_id||':PAID' WHERE g.state='SUCCEEDED' AND g.receipt IS NOT NULL"
    ):
        observed, finished = parsed_time(job["received_at"]), parsed_time(job["updated_at"])
        if not observed or not finished or finished < observed or cutoff and finished < cutoff:
            continue
        steps = store.rows(
            "SELECT spent_at,receipt,state FROM gift_steps WHERE order_id=? ORDER BY position",
            (job["order_id"],),
        )
        if not steps or any(s["state"] != "SUCCEEDED" or not s["receipt"] for s in steps):
            continue
        completed += 1
        series["observed"].append((finished - observed).total_seconds())
        started = parsed_time(steps[0]["spent_at"])
        if started and observed <= started <= finished:
            series["preparation"].append((started - observed).total_seconds())
            series["confirmation"].append((finished - started).total_seconds())
        # The current Playerok deal contract has no independently verified
        # payment timestamp. Do not silently substitute order creation time.
    for row in store.rows(
        "SELECT result,created_at FROM outbox WHERE kind='gift_send' AND status='SUCCEEDED'"
    ):
        created = parsed_time(row["created_at"])
        if cutoff and (not created or created < cutoff):
            continue
        value = json.loads(row["result"] or "{}")
        for key, duration in value.get("stage_durations", {}).items():
            if (
                key in {"deal_check", "recipient", "payment_form", "payment_rpc", "receipt_read"}
                and type(duration) in {int, float}
                and math.isfinite(duration)
                and duration >= 0
            ):
                series[key].append(duration)

    def summary(values):
        values = sorted(values)
        if not values:
            return {"samples": 0, "p50": None, "p95": None, "p99": None}
        from math import ceil

        return {
            "samples": len(values),
            **{f"p{p}": round(values[max(0, ceil(len(values) * p / 100) - 1)], 3) for p in (50, 95, 99)},
        }

    return {"completed": completed, **{key: summary(values) for key, values in series.items()}}


def amount_or_none(value):
    try:
        return money(value) if value is not None else None
    except ValueError:
        return None


def statistics(store, days=7, at=None, *, account_id=None):
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
        if account_id is not None and ((payload.get("item") or {}).get("user") or {}).get("id") != account_id:
            continue
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


# Analytics projection only: original order IDs and accounting journals stay intact.
def offer_identities(store, account):
    import unicodedata

    identities, invalid = {}, set()
    for row in store.rows("SELECT item_id,payload FROM orders"):
        payload = json.loads(row["payload"])
        item = payload.get("item") or {}
        category = item.get("category") or {}
        seller = (item.get("user") or {}).get("id")
        title = " ".join(unicodedata.normalize("NFKC", str(item.get("name") or "")).casefold().split())
        identity = {"account": seller, "category": category.get("id"), "title": title}
        ident = row["item_id"]
        if (
            seller != account
            or not category.get("id")
            or not title
            or ident in identities
            and identities[ident]["identity"] != identity
        ):
            invalid.add(ident)
        else:
            identities[ident] = {
                "identity": identity,
                "name": item["name"],
                "category": category.get("name") or "Категория",
            }
    return {key: value for key, value in identities.items() if key not in invalid}


def confirm_offer_group(store, owner, account, members, *, expected=None):
    import hashlib

    members = sorted(set(members))
    if not account or store.setting("playerok:account_id") != account or len(members) < 2:
        raise ValueError("Нужно выбрать не менее двух предложений подтверждённого магазина")
    key = f"analytics:{owner}:{account}:offer_groups"
    with store.transaction():
        if not store.rows("SELECT 1 FROM administrators WHERE user_id=?", (owner,)):
            raise ValueError("Доступ закрыт")
        identities = offer_identities(store, account)
        if any(ident not in identities for ident in members):
            raise ValueError("Не подтверждены владелец, категория или название предложения")
        if expected is not None and {ident: identities[ident] for ident in members} != expected:
            raise ValueError("Данные предложений изменились. Проверьте объединение заново")
        identity = identities[members[0]]["identity"]
        if any(identities[ident]["identity"] != identity for ident in members):
            raise ValueError("Объединять можно только предложения одного магазина, названия и категории")
        groups = store.setting(key, [])
        if any(
            set(group["members"]) & set(members) and set(group["members"]) != set(members) for group in groups
        ):
            raise ValueError("Предложение уже объединено. Сначала отмените прежнее объединение")
        group_id = hashlib.sha256(json.dumps(members).encode()).hexdigest()[:20]
        groups = [group for group in groups if group["id"] != group_id]
        groups.append({"id": group_id, "members": members, "identity": identity})
        store.set_setting(key, groups)
    return group_id


def grouped_statistics(store, owner, account, days=7):
    result = statistics(store, days, account_id=account or "")
    identities = offer_identities(store, account)
    mapping, sources = {}, {}
    if account and store.setting("playerok:account_id") == account:
        for group in store.setting(f"analytics:{owner}:{account}:offer_groups", []):
            members = group["members"]
            if all(
                ident in identities and identities[ident]["identity"] == group["identity"]
                for ident in members
            ):
                group_id = "group:" + group["id"]
                sources[group_id] = members
                mapping.update(dict.fromkeys(members, group_id))
    names, counts, unknown, amounts = {}, defaultdict(int), defaultdict(int), defaultdict(lambda: Decimal(0))
    for ident, count in result["item_orders"].items():
        target = mapping.get(ident, ident)
        names[target] = result["item_names"].get(ident, "Товар")
        counts[target] += count
        unknown[target] += result["item_unknown_paid"].get(ident, 0)
        if ident in result["items"]:
            amounts[target] += result["items"][ident]
    result.update(
        item_names=names, item_orders=counts, item_unknown_paid=unknown, items=amounts, offer_sources=sources
    )
    return result


def offer_suggestions(store, owner, account, days=7):
    """Candidates only; equality never grants an automatic merge."""
    import hashlib

    if not account or store.setting("playerok:account_id") != account:
        return {}
    identities = offer_identities(store, account)
    sold = statistics(store, days, account_id=account)["item_orders"]
    assigned = {
        ident
        for group in store.setting(f"analytics:{owner}:{account}:offer_groups", [])
        for ident in group["members"]
    }
    buckets = defaultdict(list)
    for ident, value in identities.items():
        if ident in sold and ident not in assigned:
            identity = value["identity"]
            buckets[(identity["account"], identity["category"], identity["title"])].append(ident)
    result = {}
    for members in buckets.values():
        if len(members) < 2:
            continue
        members.sort()
        key = hashlib.sha256(json.dumps(members).encode()).hexdigest()[:20]
        result[key] = {
            "members": members,
            "name": identities[members[0]]["name"],
            "expected": {ident: identities[ident] for ident in members},
        }
    return result
