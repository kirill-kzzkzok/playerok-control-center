import json
from dataclasses import asdict
from datetime import UTC, datetime

from app.database.store import Conflict, encode, now
from app.services.automation import Automation
from app.services.finance import money
from app.services.products import ValidationError, priority_price_basis
from app.services.promotions import PromotionPolicy, dry_run


class PromotionSettings:
    """Desired settings + read-only observations; never calls a paid mutation."""

    def __init__(self, store, api):
        self.store, self.api, self.access = store, api, Automation(store, api)

    def get(self):
        rows = self.store.rows("SELECT * FROM promotion_policy WHERE id=1")
        if not rows:
            return 0, PromotionPolicy(), "selected"
        return rows[0]["revision"], PromotionPolicy(**json.loads(rows[0]["payload"])), rows[0]["selection"]

    def save(self, actor, revision, values, selection):
        self.access.orders.owner(actor)
        policy = PromotionPolicy(**values)
        if selection not in {"selected", "all"}:
            raise ValidationError("Неизвестный выбор лотов")
        with self.store.transaction() as db:
            row = db.execute("SELECT revision FROM promotion_policy WHERE id=1").fetchone()
            if (row[0] if row else 0) != revision:
                raise Conflict("Настройки продвижения изменились. Откройте их заново")
            db.execute(
                "INSERT INTO promotion_policy VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,actor=excluded.actor,payload=excluded.payload,selection=excluded.selection,updated_at=excluded.updated_at",
                (revision + 1, actor, encode(asdict(policy)), selection, now()),
            )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'PROMOTION_POLICY','*','DRY_RUN_ONLY',?)",
                (actor, now()),
            )
        return revision + 1

    def stop(self, actor):
        revision, policy, selection = self.get()
        self.save(actor, revision, {**asdict(policy), "enabled": False}, selection)

    async def target(self, actor, ident, disposition):
        await self.access.item(actor, ident)
        if disposition not in {"include", "exclude", "default"}:
            raise ValidationError("Неизвестный выбор товара")
        with self.store.transaction() as db:
            if disposition == "default":
                db.execute("DELETE FROM promotion_targets WHERE item_id=?", (ident,))
            else:
                db.execute(
                    "INSERT INTO promotion_targets VALUES(?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET disposition=excluded.disposition,actor=excluded.actor,updated_at=excluded.updated_at",
                    (ident, disposition, actor, now()),
                )
        self.store.audit(actor, "PROMOTION_TARGET", ident, disposition)

    def selected(self, ident, selection):
        rows = self.store.rows("SELECT disposition FROM promotion_targets WHERE item_id=?", (ident,))
        return rows[0]["disposition"] == "include" if rows else selection == "all"

    async def evaluate(self, actor, ident, quote_id):
        item = await self.access.item(actor, ident)
        revision, policy, selection = self.get()
        checked = now()
        quotes = await self.api.call(
            "get_item_priority_statuses", item_id=ident, item_price=priority_price_basis(item)
        )
        matches = [quote for quote in quotes if quote.get("id") == quote_id]
        if len(matches) != 1 or matches[0].get("price") is None:
            raise ValidationError("Стоимость выбранного размещения не подтверждена")
        quote = {"price": str(money(matches[0]["price"])), "checked_at": checked}
        position = item.get("priority_position")
        if type(position) is not int or position < 1:
            position = None
        self.store.execute(
            "INSERT INTO promotion_observations(item_id,policy_revision,position,checked_at,source) VALUES(?,?,?,?,?)",
            (ident, revision, position, checked, "Playerok item.priority_position (priority scope)"),
        )
        at = datetime.now(UTC)
        result = dry_run(self.store, ident, quote, policy, at)
        reasons = result["reasons"]
        if not self.selected(ident, selection):
            reasons.append("лот не выбран или исключён")
        if item.get("status") != "APPROVED":
            reasons.append("лот не активен")
        if self.store.rows(
            "SELECT 1 FROM outbox WHERE target=? AND status IN ('PENDING','PROCESSING','UNCERTAIN','RETRYABLE_FAILED')",
            (ident,),
        ):
            reasons.append("у лота есть незавершённая операция")
        rows = self.store.rows(
            "SELECT created_at FROM promotion_expenses WHERE item_id=? AND status IN ('RESERVED','SUCCEEDED','UNCERTAIN') ORDER BY created_at DESC LIMIT 1",
            (ident,),
        )
        last = datetime.fromisoformat(rows[0]["created_at"]) if rows else None
        observations = self.store.rows(
            "SELECT position,checked_at FROM promotion_observations WHERE item_id=? AND policy_revision=? ORDER BY id DESC LIMIT 2",
            (ident, revision),
        )
        positions = [
            (row["position"], datetime.fromisoformat(row["checked_at"])) for row in reversed(observations)
        ]
        if not policy.due(at, last, positions):
            reasons.append("интервал, cooldown или две свежие проверки позиции не выполнены")
        result.update(
            would_allow=not reasons,
            policy_revision=revision,
            quote_id=quote_id,
            source=observations[0]["checked_at"],
            position=position,
            position_scope="priority_position; не доказательство общего места в поиске",
            paid_execution_available=False,
        )
        budget_reasons = {
            "лимит на одну операцию",
            "дневной лимит",
            "месячный лимит",
            "лимит операций на лот",
        }
        result["policy_auto_disabled"] = False
        with self.store.transaction() as db:
            if policy.enabled and budget_reasons.intersection(reasons):
                changed = db.execute(
                    "UPDATE promotion_policy SET payload=?,revision=revision+1,updated_at=? WHERE id=1 AND revision=?",
                    (encode({**asdict(policy), "enabled": False}), checked, revision),
                ).rowcount
                result["policy_auto_disabled"] = bool(changed)
                if changed:
                    db.execute(
                        "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'PROMOTION_BUDGET_GUARD',?,'LOCAL_POLICY_DISABLED',?)",
                        (actor, ident, checked),
                    )
            db.execute(
                "INSERT INTO promotion_evaluations(item_id,policy_revision,result,checked_at) VALUES(?,?,?,?)",
                (ident, revision, encode(result), checked),
            )
        return result
