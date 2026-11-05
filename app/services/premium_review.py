"""An owner may account for a debit without declaring an unknown bump successful."""

import json
from datetime import datetime, timedelta

from app.database.store import Conflict, now
from app.playerok.adapter import APIError, validate_page
from app.services.finance import money
from app.services.promotion_settings import PromotionSettings
from app.services.reconciliation import Reconciliation, signature
from playerokapi.enums import TransactionOperations

PREFIX = "promotions:review:"
UNKNOWN = "PREMIUM_PAYMENT_RESULT_UNKNOWN"


def terminal_bump(row):
    return bool(
        row["kind"] == "premium_bump"
        and row["status"] == "UNCERTAIN"
        and row["error_code"] == UNKNOWN
        and row["attempts"] == 1
        and row["started_at"]
        and row["lease_until"] is None
    )


def uncertain_expenses(store):
    return store.rows(
        "SELECT o.*,e.amount,e.item_id,EXISTS(SELECT 1 FROM outbox_late_results l WHERE l.operation_id=o.id) AS late "
        "FROM promotion_expenses e LEFT JOIN outbox o USING(operation_key) WHERE e.status='UNCERTAIN' "
        "ORDER BY e.created_at,o.id"
    )


def reviewed(store, row):
    if not terminal_bump(row) or row["late"]:
        return False
    record = store.setting(PREFIX + row["id"], {})
    return bool(
        record.get("debit_id")
        and record.get("signature") == signature(row)
        and record.get("started_at") == row["started_at"]
        and record.get("amount") == row["amount"]
    )


def blocking_expenses(store):
    return [row for row in uncertain_expenses(store) if not reviewed(store, row)]


def release_ids(store):
    return {row["id"] for row in uncertain_expenses(store) if reviewed(store, row)}


def debit_reviewed_elsewhere(store, debit, operation):
    return bool(
        store.rows(
            "SELECT 1 FROM settings WHERE key LIKE 'promotions:review:%' AND key<>? "
            "AND json_extract(value,'$.debit_id')=? LIMIT 1",
            (PREFIX + str(operation), debit),
        )
    )


class PremiumReview:
    def __init__(self, store, api):
        self.store, self.api = store, api

    async def observe(self, row):
        if not terminal_bump(row) or row["late"]:
            raise Conflict("Нет доказательства, что исходный запрос завершил работу без повтора")
        payload = json.loads(row["payload"])
        intent = self.store.setting("promotions:intent:" + row["id"], {})
        if (
            intent.get("operation_id") != row["id"]
            or intent.get("pending") is not True
            or not intent.get("started_at")
            or money(payload.get("amount")) != money(row["amount"])
        ):
            raise Conflict("Не сохранились исходные параметры платежа")
        started = datetime.fromisoformat(row["started_at"])
        end = started + timedelta(seconds=30)
        viewer = await self.api.call("get")
        if not viewer.get("id") or not (
            viewer["id"] == payload["account_id"] == self.store.setting("playerok:account_id")
        ):
            raise Conflict("Аккаунт платежа не подтверждён")
        item = await self.api.call("get_item", id=row["target"])
        if item.get("id") != row["target"] or (item.get("user") or {}).get("id") != viewer["id"]:
            raise Conflict("Владелец лота не подтверждён")
        candidate = intent.get("candidate_id")
        if candidate and candidate != payload.get("before_payment_id"):
            from app.services.paid_promotions import PaidPromotions

            service = PaidPromotions(self.store, self.api)
            try:
                if item.get("status") == "SOLD":
                    await service.historical_receipt(row, payload, item, candidate)
                else:
                    service.receipt({**payload, "operation_id": row["id"]}, item, candidate)
            except (APIError, Conflict):
                pass
            else:
                raise Conflict("Есть точная квитанция. Используйте «Проверить результат на Playerok».")
        page = await self.api.call(
            "get_transactions",
            operation=TransactionOperations.ITEM_PREMIUM_PRIORITY,
            from_date=started,
            to_date=end,
            count=24,
        )
        transactions, following = validate_page(page, "transactions", [])
        if following:
            raise Conflict("Слишком много платежей за этот период для однозначной проверки списка")
        matches = []
        for payment in transactions:
            try:
                at = datetime.fromisoformat(payment["created_at"])
                if (
                    started <= at <= end
                    and payment["status"] == "CONFIRMED"
                    and payment["operation"] == "ITEM_PREMIUM_PRIORITY"
                    and payment["direction"] == "OUT"
                    and payment["provider_id"] in {"LOCAL", "PENDING_INCOME"}
                    and (payment.get("user") or {}).get("id") == viewer["id"]
                    and money(payment["value"]) == money(row["amount"])
                    and payment["id"] != payload.get("before_payment_id")
                ):
                    matches.append(
                        {"debit_id": payment["id"], "amount": row["amount"], "at": payment["created_at"]}
                    )
            except (KeyError, ValueError, TypeError):
                continue
        if len(matches) != 1 or not matches[0]["debit_id"]:
            raise Conflict(
                "В истории нет единственного подтверждённого списания на эту сумму в момент запроса"
            )
        return matches[0]

    async def approve(self, owner, ident, expected, evidence, revision):
        Reconciliation(self.store, self.api).owner(owner)
        row = next((r for r in blocking_expenses(self.store) if r["id"] == ident), None)
        if not row or signature(row) != expected or await self.observe(row) != evidence:
            raise Conflict("Данные платежа изменились. Откройте сверку заново")
        with self.store.transaction():
            current = next((r for r in blocking_expenses(self.store) if r["id"] == ident), None)
            settings = PromotionSettings(self.store, self.api)
            if (
                not current
                or not terminal_bump(current)
                or current["late"]
                or signature(current) != expected
                or current["started_at"] != row["started_at"]
                or settings.get()[0] != revision
                or not (
                    self.api.account.id
                    == self.store.setting("playerok:account_id")
                    == json.loads(current["payload"])["account_id"]
                )
                or self.store.rows(
                    "SELECT 1 FROM outbox WHERE kind IN ('premium_bump','premium_publish') "
                    "AND status IN ('PROCESSING','PENDING','RETRYABLE_FAILED') LIMIT 1"
                )
            ):
                raise Conflict("Состояние изменилось или есть незавершённая покупка. Решение не сохранено")
            debit = evidence["debit_id"]
            if self.store.rows(
                "SELECT 1 FROM outbox WHERE json_extract(result,'$.payment_id')=? "
                "UNION ALL SELECT 1 FROM settings WHERE key LIKE 'promotions:review:%' "
                "AND json_extract(value,'$.debit_id')=? LIMIT 1",
                (debit, debit),
            ):
                raise Conflict("Это списание уже учтено в другой операции")
            settings.stop(owner)
            self.store.set_setting(
                PREFIX + ident,
                {
                    **evidence,
                    "actor": owner,
                    "signature": expected,
                    "reviewed_at": now(),
                    "started_at": current["started_at"],
                },
            )
            self.store.audit(owner, "PREMIUM_FINANCIAL_REVIEW", ident, "COUNTED_AS_EXPENSE_OUTCOME_UNKNOWN")
