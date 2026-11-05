import json
from dataclasses import asdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

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
            row = db.execute("SELECT * FROM promotion_policy WHERE id=1").fetchone()
            if (row["revision"] if row else 0) != revision:
                raise Conflict("Настройки продвижения изменились. Откройте их заново")
            old = json.loads(row["payload"]) if row else {}
            financial = {"enabled", "per_operation", "daily", "monthly", "max_operations_per_item"}
            preserve = bool(
                row
                and old.get("enabled") is True
                and policy.enabled
                and row["selection"] == selection
                and all(old.get(key) == asdict(policy)[key] for key in financial)
            )
            db.execute(
                "INSERT INTO promotion_policy VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,actor=excluded.actor,payload=excluded.payload,selection=excluded.selection,updated_at=excluded.updated_at",
                (revision + 1, actor, encode(asdict(policy)), selection, now()),
            )
            if preserve:
                for setting in self.store.rows(
                    "SELECT key,value FROM settings WHERE key='promotions:paid_authority' "
                    "OR key LIKE 'promotions:approved:%' OR key LIKE 'promotions:launch_authority:%'"
                ):
                    permission = json.loads(setting["value"])
                    field = (
                        "policy_revision" if setting["key"].startswith("promotions:approved:") else "revision"
                    )
                    if (
                        permission.get(field) == revision
                        and permission.get("account_id") == self.api.account.id
                    ):
                        self.store.set_setting(setting["key"], {**permission, field: revision + 1})
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'PROMOTION_POLICY','*','SETTINGS_SAVED_NO_PAYMENT',?)",
                (actor, now()),
            )
        return revision + 1

    def stop(self, actor):
        revision, policy, selection = self.get()
        self.save(actor, revision, {**asdict(policy), "enabled": False}, selection)
        self.store.set_setting("promotions:paid_authority", {})

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
        position = item.get("sequence")
        if type(position) is not int or position < 1:
            position = None
        self.store.execute(
            "INSERT INTO promotion_observations(item_id,policy_revision,position,checked_at,source) VALUES(?,?,?,?,?)",
            (ident, revision, position, checked, "Playerok item.sequence (listing category position)"),
        )
        at = datetime.now(UTC)
        result = dry_run(self.store, ident, quote, policy, at)
        reasons = result["reasons"]
        if not self.selected(ident, selection):
            reasons.append("лот не выбран или исключён")
        if item.get("status") != "APPROVED":
            reasons.append("лот не активен")
        from app.services.premium_review import release_ids

        released = release_ids(self.store)
        if any(
            row["id"] not in released
            for row in self.store.rows(
                "SELECT id FROM outbox WHERE target=? AND status IN ('PENDING','PROCESSING','UNCERTAIN','RETRYABLE_FAILED')",
                (ident,),
            )
        ):
            reasons.append("у лота есть незавершённая операция")
        rows = self.store.rows(
            "SELECT created_at FROM promotion_expenses WHERE item_id=? AND status IN ('RESERVED','SUCCEEDED','UNCERTAIN') ORDER BY created_at DESC LIMIT 1",
            (ident,),
        )
        last = datetime.fromisoformat(rows[0]["created_at"]) if rows else None
        observations = self.store.rows(
            "SELECT position,checked_at FROM promotion_observations WHERE item_id=? AND policy_revision=? AND source=? ORDER BY id DESC LIMIT 2",
            (ident, revision, "Playerok item.sequence (listing category position)"),
        )
        positions = [
            (row["position"], datetime.fromisoformat(row["checked_at"])) for row in reversed(observations)
        ]
        if not policy.due(at, last, positions):
            local = at.astimezone(ZoneInfo("Europe/Moscow"))
            start, end = policy.night_start_hour, policy.night_end_hour
            night = policy.night_enabled and (
                (local.hour >= start or local.hour < end) if start > end else start <= local.hour < end
            )
            threshold = policy.night_threshold if night else policy.threshold
            if policy.mode == "position" and type(position) is int and position <= threshold:
                reasons.append(f"Позиция в категории: {position}. Поднимем, когда станет ниже {threshold}-й")
            elif policy.mode == "position" and position is None:
                reasons.append("Позиция в категории не получена от Playerok; покупка по позиции недоступна")
            elif policy.mode == "position":
                reasons.append("Ожидаем две свежие проверки позиции и окончание паузы между покупками")
            else:
                reasons.append("Ожидаем окончания интервала после предыдущей покупки")
        result.update(
            would_allow=not reasons,
            policy_revision=revision,
            quote_id=quote_id,
            source=observations[0]["checked_at"],
            position=position,
            position_scope="sequence; позиция в категории из карточки Playerok",
            paid_execution_available=True,
            item_name=item.get("name"),
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
                db.execute(
                    "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) VALUES(?,'promotion_guard',?,?,?)",
                    (
                        f"promotion_guard:{revision}:{ident}",
                        encode(
                            {
                                "item_id": ident,
                                "revision": revision,
                                "reasons": [reason for reason in reasons if reason in budget_reasons],
                            }
                        ),
                        checked,
                        checked,
                    ),
                )
            db.execute(
                "INSERT INTO promotion_evaluations(item_id,policy_revision,result,checked_at) VALUES(?,?,?,?)",
                (ident, revision, encode(result), checked),
            )
        return result
