"""Local stock management; remote calls only verify the selected item's owner/state."""

import hashlib
import json
from dataclasses import asdict

from app.database.store import Conflict, encode, now
from app.playerok.adapter import APIError
from app.services.automation import Automation, DeliveryPolicy
from app.services.products import ValidationError

STATES = ("AVAILABLE", "RESERVED", "DELIVERED", "QUARANTINED")
LABELS = dict(zip(STATES, ("Доступно", "В резерве", "Выдано", "Карантин")))


def stock_values(text):
    if not isinstance(text, str) or len(text) > 10000:
        raise ValidationError("Запас: до 10000 символов, одна единица на строку")
    lines = text.splitlines()
    values = [line.strip() for line in lines if line.strip()]
    if not values:
        raise ValidationError("Нет непустых единиц товара")
    if any(len(value.encode("utf-16-le")) // 2 > 3500 for value in values):
        raise ValidationError("Единица товара превышает 3500 символов")
    unique = list(dict.fromkeys(values))
    return unique, len(lines) - len(values), len(values) - len(unique)


class Inventory:
    def __init__(self, store, api):
        self.store, self.automation = store, Automation(store, api)

    def scope(self, ident):
        from app.services.logical_products import stock_scope

        return stock_scope(self.store, ident)

    async def item(self, owner, ident):
        from app.services.logical_products import LogicalProducts
        from app.services.product_readiness import ProductReadiness

        self.automation.orders.owner(owner)
        api = self.automation.api
        account = api.account.id
        bound = self.store.setting("playerok:account_id")
        if not account or bound and bound != account:
            raise Conflict("Подключённый аккаунт Playerok изменился; склад закрыт")
        readiness = ProductReadiness(self.store, api)
        if LogicalProducts(self.store, api).listing(ident):
            item = await readiness.ui_item(owner, ident)
        else:
            version = readiness.item_version(ident)
            try:
                item = await self.automation.item(owner, ident)
            except APIError as error:
                saved = self.store.setting("readiness:" + ident, {}).get("ui_snapshot", {})
                if (
                    error.code
                    not in {
                        "CONNECTION_NOT_VERIFIED",
                        "NETWORK_ERROR",
                        "HTTP_500",
                        "HTTP_502",
                        "HTTP_503",
                        "HTTP_504",
                    }
                    or bound != account
                    or saved.get("account") != account
                    or saved.get("version") != readiness.item_version(ident)
                    or (saved.get("item") or {}).get("id") != ident
                ):
                    raise
                item = await readiness.ui_item(owner, ident)
            else:
                if api.account.id != account or self.store.setting("playerok:account_id") != bound:
                    raise Conflict("Подключённый аккаунт Playerok изменился; склад закрыт")
                if readiness.remember_item(item, version=version) is False:
                    raise readiness.superseded_read(ident, owner=owner)
        if (
            api.account.id != account
            or self.store.setting("playerok:account_id") != bound
            or item.get("id") != ident
            or (item.get("user") or {}).get("id") != account
        ):
            raise Conflict("Владелец объявления не подтверждён")
        if item.get("deleted_at") or item.get("status") not in {
            "DRAFT",
            "APPROVED",
            "DISCONTINUED",
            "PENDING_APPROVAL",
            "PENDING_MODERATION",
        }:
            raise ValidationError("Склад недоступен для текущего состояния лота; обновите его карточку")
        return item

    def counts(self, ident):
        rows = self.store.rows(
            "SELECT status,COUNT(*) AS n FROM inventory WHERE item_id=? GROUP BY status", (self.scope(ident),)
        )
        return {state: next((row["n"] for row in rows if row["status"] == state), 0) for state in STATES}

    def preview(self, ident, text):
        values, empty, duplicates = stock_values(text)
        scope = self.scope(ident)
        known = {
            row["fingerprint"]
            for row in self.store.rows("SELECT fingerprint FROM inventory WHERE item_id=?", (scope,))
        }
        fresh = [value for value in values if hashlib.sha256(value.encode()).hexdigest() not in known]
        return {
            "scope": scope,
            "new": len(fresh),
            "empty": empty,
            "duplicates": duplicates,
            "known": len(values) - len(fresh),
        }

    async def import_values(self, owner, ident, text, expected, item_status, draft_id=None):
        item = await self.item(owner, ident)
        if item["status"] != item_status:
            raise Conflict("Статус лота изменился. Подготовьте загрузку заново")
        with self.store.transaction() as db:
            if draft_id:
                draft = self.store.get_draft(draft_id, owner)
                binding = db.execute(
                    "SELECT remote_id FROM bindings WHERE draft_id=?", (draft_id,)
                ).fetchone()
                if (
                    not binding
                    or binding[0] != ident
                    or draft["payload"].get("delivery") != "stock"
                    or draft["payload"].get("delivery_data") != text
                ):
                    raise Conflict("Подготовленный запас изменился или привязан к другому лоту")
                if db.execute("SELECT 1 FROM draft_stock_imports WHERE draft_id=?", (draft_id,)).fetchone():
                    raise Conflict("Запас этого черновика уже перенесён")
            current = self.preview(ident, text)
            if current != expected:
                raise Conflict("Запас изменился после превью. Проверьте количество заново")
            values, _, _ = stock_values(text)
            count = 0
            for value in values:
                count += db.execute(
                    "INSERT OR IGNORE INTO inventory(item_id,secret,fingerprint) VALUES(?,?,?)",
                    (current["scope"], value, hashlib.sha256(value.encode()).hexdigest()),
                ).rowcount
            from app.services.orders import Orders

            Orders.stock_level(db, ident)
            if draft_id:
                db.execute(
                    "INSERT INTO draft_stock_imports VALUES(?,?,?,?,?)",
                    (draft_id, ident, owner, count, now()),
                )
                db.execute(
                    "INSERT OR IGNORE INTO delivery_policies VALUES(?,?,?,?,?)",
                    (ident, 1, owner, encode(asdict(DeliveryPolicy(mode="stock"))), now()),
                )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'IMPORT_STOCK',?,?,?)",
                (owner, ident, str(count), now()),
            )
        if count:
            from app.security.logging import event
            from app.services.logical_products import LogicalProducts

            product = LogicalProducts(self.store, self.automation.api).listing(ident)
            label = f"[{product['code']}]" if product else "Товар"
            event(
                "ORDER",
                f"📦 {label} — склад пополнен: +{count} шт., доступно {self.counts(ident)['AVAILABLE']} шт.",
            )
        return count

    def prepared(self, owner, ident):
        rows = self.store.rows(
            "SELECT d.id,d.payload FROM drafts d JOIN bindings b ON b.draft_id=d.id "
            "WHERE d.owner_id=? AND b.remote_id=? AND NOT EXISTS(SELECT 1 FROM draft_stock_imports s WHERE s.draft_id=d.id)",
            (owner, ident),
        )
        return [
            row["id"]
            for row in rows
            if (value := json.loads(row["payload"])).get("delivery") == "stock" and value.get("delivery_data")
        ]
