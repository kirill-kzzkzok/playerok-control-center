"""Historical owner-declared costs and bounded allocations of actual expense sources."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.database.store import Conflict, encode, now
from app.services.finance import money
from app.services.settlements import sale_settlement, withdrawal_settlement
from app.services.statistics import CONFIRMED, amount_or_none, parsed_time

PRIORITIES = {"ITEM_PREMIUM_PRIORITY", "ITEM_CUSTOM_PRIORITY", "ITEM_VIP_PRIORITY", "ITEM_DEFAULT_PRIORITY"}


def expense_capacity(row):
    if row["status"] != "CONFIRMED" or row["direction"] != "OUT" or row["currency"] != "RUB":
        return None
    value = amount_or_none(row["value"])
    if value is None:
        return None
    if row["operation"] in PRIORITIES:
        return value
    if row["operation"] == "WITHDRAW":
        settlement = withdrawal_settlement(row)
        return settlement["retained"] if settlement else None
    return None


def declared_amount(value):
    result = money(value)
    if result > 1_000_000_000:
        raise ValueError("Расход одного заказа должен быть не более 1 млрд ₽")
    return str(result)


def economic_fields(row):
    if row is None:
        return None
    return {
        key: row[key]
        for key in (
            "id",
            "operation",
            "direction",
            "status",
            "value",
            "currency",
            "completed_at",
            "deal_id",
            "withdrawal_net_rub",
            "reported_fee",
        )
    }


def source_fingerprint(row):
    return hashlib.sha256(encode(economic_fields(row)).encode()).hexdigest()


class OrderFinance:
    def __init__(self, store, account_id):
        self.store, self.account_id = store, account_id

    def snapshot_product_cost(self, db, order):
        """Reporting only: dated owner declaration, never a spending permission."""
        from app.services.logical_products import LogicalProducts

        item = order.get("item") or {}
        created = parsed_time(order.get("created_at"))
        if (
            order.get("status") not in {"PAID", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"}
            or order.get("_created_at_conflict")
            or not created
            or not self.account_id
            or (item.get("user") or {}).get("id") != self.account_id
        ):
            return
        if db.execute("SELECT 1 FROM order_costs WHERE order_id=?", (order["id"],)).fetchone():
            return
        product = LogicalProducts(self.store).listing(item["id"])
        if product and product["account_id"] != self.account_id:
            return
        scope = product["id"] if product else item["id"]
        owners = [row["user_id"] for row in db.execute("SELECT user_id FROM administrators").fetchall()]
        if product:
            owners = [owner for owner in owners if owner == product["owner_id"]]
        declarations = []
        for owner in owners:
            cost = None
            basis = self.store.setting(f"finance:{owner}:cost_basis:{scope}", {}) or {}
            if isinstance(basis, dict) and basis.get("account") == self.account_id:
                declared = parsed_time(basis.get("declared_at"))
                value = amount_or_none(basis.get("cost"))
                if declared and declared <= created and value is not None and value <= 1_000_000_000:
                    cost = str(value)
            zero = self.store.setting(f"finance:{owner}:other_basis:{scope}", {})
            zero_at = parsed_time(zero.get("declared_at")) if isinstance(zero, dict) else None
            other = (
                "0.00" if zero_at and zero.get("account") == self.account_id and zero_at <= created else None
            )
            if (cost is not None or other is not None) and created <= datetime.now(UTC) + timedelta(
                minutes=5
            ):
                declarations.append(
                    (owner, cost, other, zero.get("declared_at") if other is not None else None)
                )
        if len(declarations) != 1:
            return
        owner, cost, other, zero_at = declarations[0]
        inserted = db.execute(
            "INSERT OR IGNORE INTO order_costs VALUES(?,?,?,?,?,?,?,?)",
            (order["id"], self.account_id, cost, other, 1, None, owner, now()),
        ).rowcount
        if inserted:
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'SNAPSHOT_PRODUCT_COST',?,?,?)",
                (
                    owner,
                    order["id"],
                    "DECLARED_COST_ONLY" if zero_at is None else "DECLARED_OTHER_ZERO:" + zero_at,
                    now(),
                ),
            )

    def context(self, db, actor, order_id):
        if not db.execute("SELECT 1 FROM administrators WHERE user_id=?", (actor,)).fetchone():
            raise Conflict("Доступ закрыт")
        row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row or not self.account_id:
            raise Conflict("Заказ или аккаунт не подтверждён")
        payload = json.loads(row["payload"])
        seller = ((payload.get("item") or {}).get("user") or {}).get("id")
        if payload.get("direction") != "OUT" or seller != self.account_id:
            raise Conflict("Владелец сохранённой продажи не подтверждён")
        basis = db.execute("SELECT * FROM order_costs WHERE order_id=?", (order_id,)).fetchone()
        if basis and basis["account_id"] != self.account_id:
            raise Conflict("Учёт заказа привязан к другому аккаунту")
        return dict(row), dict(basis) if basis else {
            "cost": None,
            "other_expenses": None,
            "revision": 0,
            "review_fingerprint": None,
        }

    def snapshot(self, db, actor, order_id):
        order, basis = self.context(db, actor, order_id)
        payload = json.loads(order["payload"])
        linked_id = (payload.get("transaction") or {}).get("id")
        if not isinstance(linked_id, str):
            linked_id = None
        receipts = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM account_transactions WHERE account_id=? AND "
                "(deal_id=? OR (id=? AND (deal_id IS NULL OR deal_id=?))) "
                "AND operation='SELL' AND direction='IN' AND status='CONFIRMED'",
                (self.account_id, order_id, linked_id, order_id),
            ).fetchall()
        ]
        errors, receipt = [], receipts[0] if len(receipts) == 1 else None
        if order["status"] not in CONFIRMED:
            errors.append("Сделка ещё не подтверждена или возвращена")
        if len(receipts) != 1:
            errors.append("Нужно одно однозначное подтверждённое SELL-зачисление с ID этого заказа")
        shared_receipt = bool(
            receipt
            and db.execute(
                "SELECT 1 FROM orders WHERE id<>? AND json_extract(payload,'$.transaction.id')=? LIMIT 1",
                (order_id, receipt["id"]),
            ).fetchone()
        )
        if shared_receipt:
            errors.append("То же зачисление связано с другой сохранённой сделкой; нужна сверка")
        credit = amount_or_none(receipt["value"]) if receipt else None
        if receipt and (receipt["currency"] != "RUB" or credit is None):
            errors.append("Рублёвая сумма зачисления неизвестна")
            credit = None
        if receipt and parsed_time(receipt["completed_at"]) is None:
            errors.append("Нет серверной даты завершения зачисления")
        settlement = sale_settlement(order["amount"], receipt) if not errors else None
        cost, other = amount_or_none(basis["cost"]), amount_or_none(basis["other_expenses"])
        if cost is None:
            errors.append("Не задана историческая себестоимость всего заказа")
        if other is None:
            errors.append("Не задана сумма остальных расходов (явно укажите 0, если их нет)")
        allocations, allocated, sources = [], Decimal(0), []
        for row in db.execute(
            "SELECT * FROM order_expense_allocations WHERE account_id=? AND order_id=? ORDER BY transaction_id",
            (self.account_id, order_id),
        ).fetchall():
            row = dict(row)
            source = db.execute(
                "SELECT * FROM account_transactions WHERE account_id=? AND id=?",
                (self.account_id, row["transaction_id"]),
            ).fetchone()
            source = dict(source) if source else None
            capacity = expense_capacity(source) if source else None
            total = sum(
                (
                    money(item["amount"])
                    for item in db.execute(
                        "SELECT amount FROM order_expense_allocations WHERE account_id=? AND transaction_id=?",
                        (self.account_id, row["transaction_id"]),
                    ).fetchall()
                ),
                Decimal(0),
            )
            if capacity is None or total > capacity:
                errors.append("Источник распределённого расхода изменился; нужна повторная сверка")
            allocated += money(row["amount"])
            allocations.append({**row, "operation": source["operation"] if source else "UNKNOWN"})
            sources.append({"allocation": row["amount"], "source": economic_fields(source)})
        fingerprint = hashlib.sha256(
            encode(
                {
                    "account": self.account_id,
                    "order": order_id,
                    "status": order["status"],
                    "gross": order["amount"],
                    "cost": basis["cost"],
                    "other": basis["other_expenses"],
                    "receipt": economic_fields(receipt),
                    "sources": sources,
                }
            ).encode()
        ).hexdigest()
        # observed_at is local polling metadata, not a changed economic fact.
        reviewed = basis["review_fingerprint"] == fingerprint
        candidate = credit - cost - other - allocated if not errors else None
        profit = candidate if reviewed else None
        return {
            "order_id": order_id,
            "basis": basis,
            "receipt": receipt,
            "shared_receipt": shared_receipt,
            "credit": credit,
            "cost": cost,
            "other_expenses": other,
            "allocated": allocated,
            "allocations": allocations,
            "errors": errors,
            "fingerprint": fingerprint,
            "reviewed": reviewed,
            "profit": profit,
            "candidate_profit": candidate,
            "settlement": settlement,
        }

    def view(self, actor, order_id):
        with self.store.transaction() as db:
            return self.snapshot(db, actor, order_id)

    def save_basis(self, db, actor, order_id, basis, **changes):
        value = {**basis, **changes}
        db.execute(
            "INSERT INTO order_costs VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET "
            "cost=excluded.cost,other_expenses=excluded.other_expenses,revision=excluded.revision,"
            "review_fingerprint=excluded.review_fingerprint,actor=excluded.actor,updated_at=excluded.updated_at",
            (
                order_id,
                self.account_id,
                value["cost"],
                value["other_expenses"],
                basis["revision"] + 1,
                value["review_fingerprint"],
                actor,
                now(),
            ),
        )

    def check_revision(self, basis, revision):
        if basis["revision"] != revision:
            raise Conflict("Учёт изменился. Откройте карточку заново")

    def set_cost(self, actor, order_id, revision, field, value):
        if field not in {"cost", "other_expenses"}:
            raise ValueError("Неизвестный расход")
        value = declared_amount(value) if value is not None else None
        with self.store.transaction() as db:
            _, basis = self.context(db, actor, order_id)
            self.check_revision(basis, revision)
            self.save_basis(db, actor, order_id, basis, **{field: value, "review_fingerprint": None})
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'ORDER_COST',?,?,?)",
                (actor, order_id, "OWNER_DECLARED:" + field, now()),
            )

    def allocate(self, actor, order_id, revision, transaction_id, value, *, expected_source=None):
        value = money(declared_amount(value))
        with self.store.transaction() as db:
            _, basis = self.context(db, actor, order_id)
            self.check_revision(basis, revision)
            source = db.execute(
                "SELECT * FROM account_transactions WHERE account_id=? AND id=?",
                (self.account_id, transaction_id),
            ).fetchone()
            if expected_source is not None and source_fingerprint(source) != expected_source:
                raise Conflict("Источник расхода изменился после превью")
            capacity = expense_capacity(source) if source else None
            if not source or (value and capacity is None):
                raise Conflict("Подтверждённый источник расхода недоступен")
            others = sum(
                (
                    money(row[0])
                    for row in db.execute(
                        "SELECT amount FROM order_expense_allocations WHERE account_id=? AND transaction_id=? AND order_id<>?",
                        (self.account_id, transaction_id, order_id),
                    ).fetchall()
                ),
                Decimal(0),
            )
            if value and value + others > capacity:
                raise Conflict("Сумма распределений превышает подтверждённый расход")
            if value:
                db.execute(
                    "INSERT INTO order_expense_allocations VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,transaction_id,order_id) DO UPDATE SET amount=excluded.amount,actor=excluded.actor,updated_at=excluded.updated_at",
                    (self.account_id, transaction_id, order_id, str(value), actor, now()),
                )
            else:
                db.execute(
                    "DELETE FROM order_expense_allocations WHERE account_id=? AND transaction_id=? AND order_id=?",
                    (self.account_id, transaction_id, order_id),
                )
            self.save_basis(db, actor, order_id, basis, review_fingerprint=None)
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'ALLOCATE_EXPENSE',?,'OWNER_DECLARED',?)",
                (actor, order_id, now()),
            )

    def review(self, actor, order_id, revision, expected):
        with self.store.transaction() as db:
            value = self.snapshot(db, actor, order_id)
            self.check_revision(value["basis"], revision)
            if value["errors"] or value["fingerprint"] != expected:
                raise Conflict("Исходные данные изменились или неполны; проверьте карточку")
            self.save_basis(db, actor, order_id, value["basis"], review_fingerprint=expected)
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'REVIEW_ORDER_COST',?,'OWNER_ATTESTATION',?)",
                (actor, order_id, now()),
            )

    def expense_sources(self, actor, order_id, page=0):
        if not 0 <= page <= 1_000_000:
            raise ValueError("Недопустимая страница")
        with self.store.transaction() as db:
            self.context(db, actor, order_id)
            # Filter eligible types in SQL; capacity can still be unknown or zero.
            rows = db.execute(
                "SELECT * FROM account_transactions WHERE account_id=? AND status='CONFIRMED' AND direction='OUT' AND currency='RUB' AND operation IN ('WITHDRAW','ITEM_PREMIUM_PRIORITY','ITEM_CUSTOM_PRIORITY','ITEM_VIP_PRIORITY','ITEM_DEFAULT_PRIORITY') ORDER BY created_at DESC,id LIMIT 9 OFFSET ?",
                (self.account_id, page * 8),
            ).fetchall()
            return [dict(row) | {"capacity": expense_capacity(row)} for row in rows[:8]], len(rows) > 8


def profit_statistics(store, actor, account_id, days=7, at=None, *, include_costs=False):
    if not store.rows("SELECT 1 FROM administrators WHERE user_id=?", (actor,)):
        raise Conflict("Доступ закрыт")
    if days not in {0, 1, 2, 7, 30}:
        raise ValueError("Недопустимый период")
    at = at or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("Требуется часовой пояс")
    start = at - timedelta(days=days) if days else datetime.min.replace(tzinfo=UTC)
    service = OrderFinance(store, account_id)
    result = {
        "profit": Decimal(0),
        "reviewed_orders": 0,
        "incomplete_orders": 0,
        "unattributed_orders": 0,
        "foreign_orders": 0,
        "known_cost": Decimal(0),
        "known_other_expenses": Decimal(0),
        "costed_sales": 0,
        "other_costed_sales": 0,
    }
    counted_receipts = set()
    for order in store.rows("SELECT id FROM orders WHERE status IN ('CONFIRMED','CONFIRMED_AUTOMATICALLY')"):
        try:
            value = service.view(actor, order["id"])
        except Conflict:
            result["foreign_orders"] += 1
            continue
        receipt = value["receipt"]
        when = parsed_time(receipt["completed_at"]) if receipt else None
        if when is None:
            result["unattributed_orders"] += 1
            continue
        if not start <= when <= at:
            continue
        if (
            not value["shared_receipt"]
            and value["credit"] is not None
            and receipt["id"] not in counted_receipts
        ):
            counted_receipts.add(receipt["id"])
            if value["cost"] is not None:
                result["known_cost"] += value["cost"]
                result["costed_sales"] += 1
            if value["other_expenses"] is not None:
                result["known_other_expenses"] += value["other_expenses"]
                result["other_costed_sales"] += 1
        if value["profit"] is None:
            result["incomplete_orders"] += 1
        else:
            result["profit"] += value["profit"]
            result["reviewed_orders"] += 1
    if not include_costs:
        for key in ("known_cost", "known_other_expenses", "costed_sales", "other_costed_sales"):
            result.pop(key)
    return result


def cache_shop_profit(store, account_id, *, partial=False):
    """Observed historical evidence only, never a spending/delivery permission."""
    from app.services.transactions import transaction_statistics

    at = datetime.now(UTC)
    start = at - timedelta(hours=24)
    ledger = transaction_statistics(store, days=1, at=at, account_id=account_id, include_open_sales=True)
    promotion = ledger["premium_debit"] + ledger["other_priority_debit"]
    uncertain = store.rows(
        "SELECT COUNT(*) AS n FROM promotion_expenses e JOIN outbox o ON o.operation_key=e.operation_key "
        "WHERE e.status='UNCERTAIN' AND json_extract(o.payload,'$.account_id')=?",
        (account_id,),
    )[0]["n"]
    seen = set()
    promotion_by_kind = {"premium_bump": Decimal(0), "premium_publish": Decimal(0)}
    for row in store.rows(
        "SELECT o.kind,e.amount,e.created_at,json_extract(o.result,'$.payment_id') AS payment_id,"
        "json_extract(o.result,'$.paid_rub') AS paid_rub,t.id AS ledger_id,t.completed_at,"
        "t.status,t.operation,t.direction,t.currency,t.value "
        "FROM promotion_expenses e JOIN outbox o ON o.operation_key=e.operation_key "
        "LEFT JOIN account_transactions t ON t.account_id=? AND t.id=json_extract(o.result,'$.payment_id') "
        "WHERE e.status='SUCCEEDED' AND o.status='SUCCEEDED' "
        "AND o.kind IN ('premium_bump','premium_publish') AND json_extract(o.payload,'$.account_id')=? "
        "ORDER BY e.created_at,o.id",
        (account_id, account_id),
    ):
        ident = row["payment_id"]
        when = parsed_time(row["completed_at"] if row["ledger_id"] else row["created_at"])
        paid = amount_or_none(row["paid_rub"])
        if (
            isinstance(ident, str)
            and ident
            and ident not in seen
            and when is not None
            and start <= when <= at
            and paid is not None
            and paid == amount_or_none(row["amount"])
        ):
            if row["ledger_id"]:
                if (row["status"], row["operation"], row["direction"], row["currency"]) != (
                    "CONFIRMED",
                    "ITEM_PREMIUM_PRIORITY",
                    "OUT",
                    "RUB",
                ) or amount_or_none(row["value"]) != paid:
                    continue
            else:
                promotion += paid
            promotion_by_kind[row["kind"]] += paid
            seen.add(ident)
    awaiting_confirmation = store.rows(
        "SELECT COUNT(*) AS n FROM orders WHERE status='SENT' "
        "AND json_extract(payload,'$.direction')='OUT' "
        "AND json_extract(payload,'$.item.user.id')=? "
        "AND julianday(created_at) BETWEEN julianday(?) AND julianday(?)",
        (account_id, start.isoformat(), at.isoformat()),
    )[0]["n"]
    for owner in store.rows("SELECT user_id FROM administrators"):
        costs = profit_statistics(store, owner["user_id"], account_id, days=1, at=at, include_costs=True)
        sales = ledger["sales_credit"]
        cost, other = costs["known_cost"], costs["known_other_expenses"]
        withdrawal = ledger["withdraw_retained_known"]
        net = sales - cost - other - promotion - withdrawal
        sales_count = ledger["sales_count"] + ledger["pending_sales_count"]
        result = {
            "sales_credit": sales,
            "cost": cost,
            "other": other,
            "promotion": promotion,
            "premium": promotion - ledger["other_priority_debit"],
            "premium_bump": promotion_by_kind["premium_bump"],
            "premium_publish": promotion_by_kind["premium_publish"],
            "premium_unclassified": promotion
            - ledger["other_priority_debit"]
            - sum(promotion_by_kind.values()),
            "other_placement": ledger["other_priority_debit"],
            "withdrawal": withdrawal,
            "net": net,
            "pending_sales": ledger["pending_sales"],
            "refunds": ledger["refunds"],
            "projected_net": net + ledger["pending_sales"],
        }
        store.set_setting(
            f"shop:profit:{account_id}:{owner['user_id']}",
            {
                **{key: f"{value:.2f}" for key, value in result.items()},
                "sales_count": sales_count,
                "awaiting_confirmation": awaiting_confirmation,
                "pending_sales_count": ledger["pending_sales_count"],
                "refund_count": ledger["refund_count"],
                "missing_cost": max(0, sales_count - costs["costed_sales"]),
                "missing_other": max(0, sales_count - costs["other_costed_sales"]),
                "unknown_withdrawals": ledger["withdraw_net_unknown"],
                "uncertain_promotions": uncertain,
                "incomplete_transactions": sum(
                    ledger[key]
                    for key in ("unknown_dates", "unknown_amounts", "unknown_directions", "foreign_currency")
                ),
                "at": at.isoformat(),
                "partial": partial,
            },
        )


def settlement_statistics(store, actor, account_id, days=7, at=None):
    if not store.rows("SELECT 1 FROM administrators WHERE user_id=?", (actor,)):
        raise Conflict("Доступ закрыт")
    if days not in {0, 1, 2, 7, 30}:
        raise ValueError("Недопустимый период")
    at = at or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("Требуется часовой пояс")
    start = at - timedelta(days=days) if days else datetime.min.replace(tzinfo=UTC)
    result = {
        "orders": 0,
        "gross": Decimal(0),
        "credit": Decimal(0),
        "fees": Decimal(0),
        "unreconciled": 0,
        "unattributed": 0,
        "foreign": 0,
    }
    service = OrderFinance(store, account_id)
    for row in store.rows("SELECT id FROM orders WHERE status IN ('CONFIRMED','CONFIRMED_AUTOMATICALLY')"):
        try:
            value = service.view(actor, row["id"])
        except Conflict:
            result["foreign"] += 1
            continue
        receipt, settlement = value["receipt"], value["settlement"]
        when = parsed_time(receipt["completed_at"]) if receipt else None
        if when is None:
            result["unattributed"] += 1
            continue
        if not start <= when <= at:
            continue
        if not settlement or not settlement["reconciled"]:
            result["unreconciled"] += 1
            continue
        result["orders"] += 1
        result["gross"] += settlement["gross"]
        result["credit"] += settlement["credit"]
        result["fees"] += settlement["fee"]
    return result
