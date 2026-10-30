"""Account-scoped read journal; no payment credentials, guessed fees or mutations."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.database.store import now
from app.playerok.adapter import APIError
from app.services.settlements import withdrawal_settlement
from app.services.statistics import amount_or_none, parsed_time


def token(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value) else "UNKNOWN"


def timestamp(value):
    parsed = parsed_time(value)
    return parsed.astimezone(UTC).isoformat() if parsed else None


def amount(value):
    parsed = amount_or_none(value)
    return str(parsed) if parsed is not None else None


class Transactions:
    def __init__(self, store, account_id):
        if not isinstance(account_id, str) or not account_id or len(account_id) > 100:
            raise ValueError("Не подтверждён аккаунт транзакций")
        self.store, self.account_id = store, account_id

    def ingest(self, entry):
        ident = entry.get("id")
        if not isinstance(ident, str) or not ident or len(ident) > 100:
            raise APIError("TRANSACTION_ID_INVALID")
        user = entry.get("user") or {}
        if user.get("id") and user["id"] != self.account_id:
            raise APIError("TRANSACTION_ACCOUNT_MISMATCH")
        props = entry.get("props") or {}
        deal_id = props.get("deal_id")
        if not isinstance(deal_id, str) or len(deal_id) > 100:
            deal_id = None
        status = token(entry.get("status"))
        # value is RUB in the verified website transaction formatter (modules 26660/57832).
        # Preserve an explicit different currency; never convert it using today's rate.
        currency = token(entry.get("currency", "RUB"))
        created, completed = timestamp(entry.get("created_at")), timestamp(entry.get("completed_at"))
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT status,created_at,completed_at FROM account_transactions WHERE account_id=? AND id=?",
                (self.account_id, ident),
            ).fetchone()
            if (
                previous
                and previous["status"] in {"CONFIRMED", "ROLLED_BACK", "FAILED"}
                and status in {"PENDING", "PROCESSING"}
            ):
                return False
            created = created or (previous["created_at"] if previous else None)
            if previous and status == previous["status"] and completed is None:
                completed = previous["completed_at"]
            db.execute(
                "INSERT INTO account_transactions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id,id) DO UPDATE SET operation=excluded.operation,direction=excluded.direction,"
                "status=excluded.status,value=excluded.value,currency=excluded.currency,created_at=excluded.created_at,"
                "completed_at=excluded.completed_at,deal_id=excluded.deal_id,reported_fee=excluded.reported_fee,"
                "fee_percent=excluded.fee_percent,withdrawal_net_rub=excluded.withdrawal_net_rub,observed_at=excluded.observed_at",
                (
                    self.account_id,
                    ident,
                    token(entry.get("operation")),
                    token(entry.get("direction")),
                    status,
                    amount(entry.get("value")),
                    currency,
                    created,
                    completed,
                    deal_id,
                    amount(entry.get("fee")),
                    amount(props.get("fee")),
                    amount(props.get("amount_after_conversion_rub")),
                    now(),
                ),
            )
        return True


def transaction_statistics(store, days=7, at=None):
    if days not in {0, 1, 2, 7, 30}:
        raise ValueError("Недопустимый период")
    at = at or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("Требуется часовой пояс")
    start = at - timedelta(days=days) if days else datetime.min.replace(tzinfo=UTC)
    account = store.setting("transactions:account_id")
    result = {
        "cached": 0,
        "confirmed": 0,
        "unknown_dates": 0,
        "unknown_amounts": 0,
        "unknown_directions": 0,
        "foreign_currency": 0,
        "unknown_operations": 0,
        "credits": Decimal(0),
        "debits": Decimal(0),
        "sales_credit": Decimal(0),
        "withdraw_debit": Decimal(0),
        "premium_debit": Decimal(0),
        "other_priority_debit": Decimal(0),
        "deposits": Decimal(0),
        "withdraw_net_reported": Decimal(0),
        "withdraw_net_unknown": 0,
        "withdraw_retained_known": Decimal(0),
        "withdraw_known_count": 0,
        "coverage_start": None,
        "coverage_end": None,
        "last_sync": None,
        "basis": "confirmed_transactions_by_completed_at",
        "has_data": False,
    }
    if not account:
        return result
    result["last_sync"] = store.setting(f"transactions:{account}:completed_at")
    coverage = []
    rows = store.rows("SELECT * FROM account_transactions WHERE account_id=?", (account,))
    result["cached"] = len(rows)
    result["has_data"] = bool(rows or result["last_sync"])
    for row in rows:
        if row["status"] != "CONFIRMED":
            continue
        when = parsed_time(row["completed_at"])
        if when is None:
            result["unknown_dates"] += 1
            continue
        if when <= at:
            coverage.append(when)
        if not start <= when <= at:
            continue
        result["confirmed"] += 1
        if row["operation"] == "WITHDRAW" and row["direction"] == "OUT":
            settlement = withdrawal_settlement(row)
            if settlement is None:
                result["withdraw_net_unknown"] += 1
            else:
                result["withdraw_net_reported"] += settlement["net"]
                result["withdraw_retained_known"] += settlement["retained"]
                result["withdraw_known_count"] += 1
        value = amount_or_none(row["value"])
        if row["currency"] != "RUB":
            result["foreign_currency"] += 1
            continue
        if value is None:
            result["unknown_amounts"] += 1
            continue
        direction, operation = row["direction"], row["operation"]
        if direction not in {"IN", "OUT"}:
            result["unknown_directions"] += 1
            continue
        result["credits" if direction == "IN" else "debits"] += value
        key = {
            ("SELL", "IN"): "sales_credit",
            ("DEPOSIT", "IN"): "deposits",
            ("WITHDRAW", "OUT"): "withdraw_debit",
            ("ITEM_PREMIUM_PRIORITY", "OUT"): "premium_debit",
            ("ITEM_CUSTOM_PRIORITY", "OUT"): "other_priority_debit",
            ("ITEM_VIP_PRIORITY", "OUT"): "other_priority_debit",
            ("ITEM_DEFAULT_PRIORITY", "OUT"): "other_priority_debit",
        }.get((operation, direction))
        if key:
            result[key] += value
        else:
            result["unknown_operations"] += 1
    if coverage:
        result.update(coverage_start=min(coverage).isoformat(), coverage_end=max(coverage).isoformat())
    return result
