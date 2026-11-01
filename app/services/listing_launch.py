"""PREPARE (read-only checks) then a single atomic COMMIT for a listing launch.

Design (PROGRESS.md, "STOP-THE-PATCH-CASCADE"):
- Stage 1 PREPARE: only reads (server/Playerok); no local writes.
- Stage 2 COMMIT: one BEGIN IMMEDIATE transaction holding EVERY local write:
  delivery configuration, paid permissions, the Outbox operation, the durable
  marker, the DRAFT->QUEUED CAS. Partial local state cannot survive a failure:
  SQLite rolls the whole transaction back. This removes the snapshot/rollback
  machinery and its recurring defect class entirely.
- The only PREPARE-stage local write is the promotion schedule enable flag
  (non-spending: every paid operation still needs approval + authority), which
  the premium preview must observe for consistent revision checks. The stock
  import moved INTO the commit transaction (idempotent by fingerprint).
"""

import hashlib
import json
from dataclasses import asdict

from app.database.store import Conflict, encode, now
from app.services.automation import Automation, DeliveryPolicy, message_text
from app.services.finance import money
from app.services.inventory import Inventory, stock_values
from app.services.paid_promotions import PaidPromotions
from app.services.products import Products, ValidationError, priority_price_basis
from app.services.promotion_settings import PromotionSettings
from app.services.promotions import PromotionPolicy


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def launch_key(ident, revision):
    """Revision in the operation key: a retry after PERMANENT_FAILED (which bumps the
    draft revision) queues a fresh operation instead of returning the dead one."""
    return f"launch:{ident}:{revision}"


def launch_prefix(ident):
    """Exact LIKE prefix for all revisions of one launch draft; ids are hex so the
    pattern contains no SQL wildcards."""
    return launch_key(ident, "%")


class ListingLaunch:
    def __init__(self, store, api, gifts):
        self.store, self.api, self.gifts = store, api, gifts
        self.products = Products(store, api)
        self.automation = Automation(store, api)

    def load(self, owner, ident):
        self.automation.orders.owner(owner)
        draft = self.store.get_draft(ident, owner)
        if not draft["payload"].get("launch_target"):
            raise Conflict("Это не настройка запуска товара")
        return draft

    async def open(self, owner, target, source=None):
        item = await self.automation.item(owner, target)
        if item.get("status") != "DRAFT":
            raise ValidationError("Настройка запуска доступна до публикации. Обновите карточку товара")
        key = "launch:config:" + target
        with self.store.transaction():
            ident = self.store.setting(key)
            if ident:
                self.load(owner, ident)
                return ident
            value = source or {}
            order, day = self.gifts.limits()
            mode = value.get("delivery", "manual")
            if mode not in {"manual", "text", "stock", "gifts"}:
                mode = "manual"
            ident = self.store.draft(
                owner,
                {
                    "launch_target": target,
                    "account_id": self.api.account.id,
                    "mode": mode,
                    "data": value.get("delivery_data", ""),
                    "greeting": "",
                    "after": "",
                    "auto_complete": False,
                    "refund": False,
                    "order_limit": order,
                    "day_limit": day,
                    "publication": "DEFAULT",
                    "quote_id": None,
                    "promotion": False,
                    "promotion_policy": asdict(PromotionSettings(self.store, self.api).get()[1]),
                },
            )
            self.store.set_setting(key, ident)
            return ident

    async def check(self, owner, value):
        if self.api.account.id != value["account_id"]:
            raise ValidationError("Аккаунт Playerok изменился. Откройте новый черновик запуска")
        PaidPromotions(self.store, self.api).permissions(value["account_id"])
        item = await self.automation.item(owner, value["launch_target"])
        if item.get("status") != "DRAFT" or item.get("deleted_at"):
            raise ValidationError(
                "Товар уже не является неопубликованным черновиком; нужна сверка результата"
            )
        mode = value["mode"]
        if mode not in {"manual", "text", "stock", "gifts"}:
            raise ValidationError("Выберите способ выдачи")
        if mode != "manual":
            if not getattr(self.api.account, "automation_enabled", False):
                raise ValidationError("Автоматизация выключена. Проверьте настройки подключения")
            if (
                not self.store.setting("orders:last_complete_sync")
                or self.store.setting("sync:health", {}).get("status") != "OK"
            ):
                raise ValidationError("Сначала завершите успешную синхронизацию заказов")
        message_text(value["greeting"])
        message_text(value["after"])
        result = {
            "item": {
                k: item.get(k)
                for k in ("id", "name", "description", "price", "raw_price", "category", "obtaining_type")
            },
            "account": value["account_id"],
            "config": digest({k: v for k, v in value.items() if k != "ready"}),
        }
        if mode in {"text", "stock"}:
            DeliveryPolicy(
                enabled=True,
                mode=mode,
                text=value["data"] if mode == "text" else "",
                greeting=value["greeting"],
                after=value["after"],
                mark_sent=value["auto_complete"],
                auto_enroll=True,
            )
            if mode == "stock":
                preview = Inventory(self.store, self.api).preview(item["id"], value["data"])
                if preview["empty"] or preview["duplicates"]:
                    raise ValidationError("В запасе есть пустые строки или повторы. Исправьте загрузку")
                result["units"] = (
                    Inventory(self.store, self.api).counts(item["id"])["AVAILABLE"] + preview["new"]
                )
                if result["units"] < 1:
                    raise ValidationError("Нет доступных единиц для выдачи")
        if mode == "gifts":
            s = self.gifts
            s.settings.require_configured()
            if not s.settings.payment_enabled:
                raise ValidationError("Покупка подарков выключена в настройках подключения")
            if value["greeting"]:
                raise ValidationError(
                    "Для подарков отдельное приветствие пока недоступно; очистите его. Итоговое сообщение настраивается ниже"
                )
            if not (
                0 < value["order_limit"] <= s.settings.max_order_stars
                and value["order_limit"] <= value["day_limit"] <= s.settings.max_daily_stars
            ):
                raise ValidationError(
                    "Лимиты Stars должны быть положительными и не превышать ограничения подключения"
                )
            data = await s.inspect_item(owner, item["id"])
            if not data["required"]:
                raise ValidationError("Поле Telegram username в способе получения должно быть обязательным")
            await s.refresh()
            bundle = s.catalog_bundle(data["code"])
            total = sum(g["stars"] for g in bundle)
            balance = self.store.setting("gifts:balance", {}).get("stars")
            reserved = sum(
                r["reserved_stars"] for r in self.store.rows("SELECT reserved_stars FROM gift_jobs")
            )
            if balance is None or balance < reserved + total:
                raise ValidationError("Недостаточно Stars с учётом зарезервированных подарков")
            if total > min(s.limits()[0], value["order_limit"]) or total > min(
                s.limits()[1], value["day_limit"]
            ):
                raise ValidationError("Стоимость пакета превышает разрешённые лимиты Stars")
            if self.store.rows("SELECT 1 FROM gift_steps WHERE state IN ('SPENDING','UNCERTAIN') LIMIT 1"):
                raise ValidationError("Предыдущая покупка подарка требует сверки; новый запуск заблокирован")
            if value["refund"] and (
                not s.settings.refund_enabled or not getattr(self.api, "gift_refund_capability", None)
            ):
                raise ValidationError(
                    "Автовозврат не подтверждён для этого подключения. Выберите ручное решение"
                )
            result.update(bundle=bundle, payer=self.store.setting("gifts:personal_account_id"))
        quotes = await self.products.priority_preview(item["id"], priority_price_basis(item))
        candidates = [
            q
            for q in quotes
            if q.get("type") == value["publication"]
            and q.get("price") is not None
            and (not value["quote_id"] or q["id"] == value["quote_id"])
        ]
        if len(candidates) != 1:
            raise ValidationError("Выберите один актуальный тариф размещения")
        quote = candidates[0]
        amount = money(quote["price"])
        if value["publication"] == "DEFAULT" and amount != 0:
            raise ValidationError("Бесплатность размещения не подтверждена")
        if value["publication"] == "PREMIUM":
            if amount <= 0:
                raise ValidationError("Цена PREMIUM не подтверждена")
            policy = PromotionPolicy(**value["promotion_policy"])
            if (
                min(money(policy.per_operation), money(policy.daily), money(policy.monthly)) < amount
                or policy.max_operations_per_item < 1
            ):
                raise ValidationError("Настройте лимиты расходов Playerok не ниже выбранного тарифа")
            # The launch budget must stay in sync with the live global policy: a stale
            # copy would silently overwrite other lots' promotion settings on queue().
            # The volatile `enabled` flag is excluded: queue() legitimately flips it for
            # the opt-in schedule without invalidating the confirmed budget itself.
            _revision, current_policy, _ = PromotionSettings(self.store, self.api).get()
            expected = dict(value["promotion_policy"])
            expected.pop("enabled", None)
            actual = asdict(current_policy)
            actual.pop("enabled", None)
            if expected != actual:
                raise Conflict(
                    "Общие настройки продвижения изменились. Откройте черновик запуска и обновите лимиты"
                )
            # policy_revision is deliberately NOT part of the readiness digest: the
            # opt-in schedule enable in queue() bumps it without changing the budget.
            viewer = await self.api.call("get")
            if (
                viewer.get("id") != value["account_id"]
                or money((viewer.get("profile") or {}).get("balance", {}).get("available", 0)) < amount
            ):
                raise ValidationError("Баланс Playerok или его владелец не подтверждены для PREMIUM")
        if value["promotion"] and value["publication"] != "PREMIUM":
            raise ValidationError("Платное автоподнятие требует подтверждённого PREMIUM-тарифа")
        result["quote"] = {k: quote.get(k) for k in ("id", "type", "price", "period")}
        return result

    async def queue(self, owner, ident, revision):
        draft = self.load(owner, ident)
        value = draft["payload"]
        if draft["revision"] != revision or draft["state"] != "DRAFT" or not value.get("ready"):
            raise Conflict("Настройки изменились. Проверьте готовность заново")
        item = value["launch_target"]

        # ---------- Stage 1: PREPARE (read-only checks; local writes only for
        # the non-spending schedule enable flag, which the premium preview must
        # see to keep revision checks consistent) ----------
        fresh = await self.check(owner, value)
        if digest(fresh) != digest(value["ready"]):
            raise Conflict("Товар, тариф или подарок изменились. Подтвердите новое превью")
        premium_preview = None
        if value["publication"] == "PREMIUM":
            if value["promotion"]:
                # The schedule enable flag is a LOCAL, non-spending write: flip it in
                # PREPARE so the premium preview (and its policy_revision) is taken
                # against the post-enable policy. Enabling the schedule never spends
                # by itself: every paid operation still requires approval+authority.
                settings = PromotionSettings(self.store, self.api)
                p_revision, existing, selection = settings.get()
                if asdict(existing) != {**value["promotion_policy"], "enabled": existing.enabled}:
                    raise Conflict("Общие настройки продвижения изменились. Подтвердите новые лимиты")
                if not existing.enabled:
                    settings.save(
                        owner, p_revision, {**value["promotion_policy"], "enabled": True}, selection
                    )
            paid = PaidPromotions(self.store, self.api)
            premium_preview = await paid.preview(owner, item, fresh["quote"]["id"])
            if money(premium_preview["amount"]) != money(fresh["quote"]["price"]):
                raise Conflict("Цена PREMIUM изменилась при подготовке. Подтвердите новое превью")

        # ---------- Stage 2: COMMIT (single atomic transaction, no awaits) ----------
        with self.store.transaction() as db:
            # CAS: this revision is never queued twice.
            if (
                db.execute(
                    "UPDATE drafts SET state='QUEUED' WHERE id=? AND revision=? AND state='DRAFT'",
                    (ident, revision),
                ).rowcount
                != 1
            ):
                raise Conflict("Запуск уже подтверждён или настройки изменились")
            delivery_revision = 0
            if value["mode"] == "gifts":
                delivery_revision = self._commit_gift_listing(db, owner, item, value, fresh)
            elif value["mode"] in {"text", "stock"}:
                delivery_revision = self._commit_text_delivery(db, owner, item, value)
            else:
                row = db.execute("SELECT enabled FROM gift_listings WHERE item_id=?", (item,)).fetchone()
                policy = db.execute(
                    "SELECT payload FROM delivery_policies WHERE item_id=?", (item,)
                ).fetchone()
                if (row and row["enabled"]) or (policy and json.loads(policy["payload"]).get("enabled")):
                    raise Conflict("Для ручного режима сначала остановите существующую выдачу товара")
            if value["publication"] == "PREMIUM":
                operation = self._commit_premium_local(
                    db, owner, item, value, fresh, premium_preview, ident, revision, delivery_revision
                )
            else:
                operation = self.store.enqueue(
                    launch_key(ident, revision),
                    "publish",
                    item,
                    {
                        "priority_id": fresh["quote"]["id"],
                        "price": fresh["item"]["price"],
                        "raw_price": priority_price_basis(fresh["item"]),
                        "launch_id": ident,
                        "launch_revision": revision,
                        "delivery_revision": delivery_revision,
                    },
                    db,
                    actor=owner,
                )
                self.store.set_setting(
                    "launch:prepared:" + item,
                    {
                        "id": ident,
                        "owner": owner,
                        "revision": revision,
                        "operation": operation,
                        "delivery_revision": delivery_revision,
                        "at": now(),
                    },
                )
            return operation

    def _commit_gift_listing(self, db, owner, item, value, fresh):
        """Synchronous gift bind + arm inside the commit transaction, from PREPARE
        data only (the bundle was network-verified in check()). Returns the armed
        listing revision."""
        existing = db.execute(
            "SELECT revision,enabled,description_sha256,gift_code FROM gift_listings WHERE item_id=?",
            (item,),
        ).fetchone()
        if existing and existing["enabled"]:
            raise Conflict("Сначала остановите выдачу этого лота")
        from app.gifts.core import gift_code_from_description
        from app.gifts.service import description_hash

        inspect_hash = description_hash(fresh["item"]["description"])
        confirmed_code = gift_code_from_description(fresh["item"]["description"])
        if existing and existing["description_sha256"] != inspect_hash:
            raise Conflict("Привязка изменилась; подтвердите её заново")
        if existing and existing["gift_code"] != confirmed_code:
            raise Conflict("Подтверждённый код не совпадает с последней строкой описания")
        bundle = fresh["bundle"]
        db.execute(
            "INSERT INTO gift_listings(item_id,owner_id,gift_code,gift_id,approved_stars,description_sha256,"
            "category_id,obtaining_type_id,username_field_id) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET gift_code=excluded.gift_code,gift_id=excluded.gift_id,"
            "approved_stars=excluded.approved_stars,description_sha256=excluded.description_sha256,"
            "category_id=excluded.category_id,obtaining_type_id=excluded.obtaining_type_id,"
            "username_field_id=excluded.username_field_id,revision=revision+1,enabled=0,armed_at=NULL",
            (
                item,
                owner,
                confirmed_code,
                bundle[0]["gift_id"],
                sum(g["stars"] for g in bundle),
                inspect_hash,
                self.gifts.settings.category_id,
                self.gifts.settings.obtaining_type_id,
                self.gifts.settings.buyer_username_field_id,
            ),
        )
        db.execute("UPDATE gift_listings SET bundle=? WHERE item_id=?", (encode(bundle), item))
        policy = db.execute("SELECT payload FROM delivery_policies WHERE item_id=?", (item,)).fetchone()
        if policy and json.loads(policy["payload"]).get("enabled"):
            raise Conflict("Для лота уже действует складская/текстовая выдача или незавершённая цепочка")
        db.execute(
            "UPDATE gift_listings SET enabled=1,auto_fulfill=?,armed_at=?,revision=revision+1 WHERE item_id=?",
            (int(value["auto_complete"]), now(), item),
        )
        db.execute(
            "INSERT INTO settings VALUES('gifts:stopped','false') ON CONFLICT(key) DO UPDATE SET value='false'"
        )
        self.store.set_setting(
            "gifts:listing_options:" + item,
            {
                "order": value["order_limit"],
                "day": value["day_limit"],
                "refund": value["refund"],
                "after": value["after"],
            },
        )
        return db.execute("SELECT revision FROM gift_listings WHERE item_id=?", (item,)).fetchone()[
            "revision"
        ]

    def _commit_text_delivery(self, db, owner, item, value):
        """Synchronous delivery-policy write inside the commit transaction."""
        row = db.execute("SELECT revision FROM delivery_policies WHERE item_id=?", (item,)).fetchone()
        version = row["revision"] if row else 0
        policy = DeliveryPolicy(
            True,
            value["mode"],
            value["data"] if value["mode"] == "text" else "",
            value["greeting"],
            value["after"],
            value["auto_complete"],
            True,
        )
        db.execute(
            "INSERT INTO delivery_policies(item_id,revision,actor,payload,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET revision=excluded.revision,actor=excluded.actor,"
            "payload=excluded.payload,updated_at=excluded.updated_at",
            (item, version + 1, owner, encode(asdict(policy)), now()),
        )
        db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"delivery:{item}:enabled", encode(True)),
        )
        if value["mode"] == "stock":
            # Idempotent stock import (fingerprint INSERT OR IGNORE), synchronous.
            for unit in stock_values(value["data"])[0]:
                db.execute(
                    "INSERT OR IGNORE INTO inventory(item_id,secret,fingerprint) VALUES(?,?,?)",
                    (item, unit, hashlib.sha256(unit.encode()).hexdigest()),
                )
        return version + 1

    def _commit_premium_local(
        self, db, owner, item, value, fresh, preview, ident, revision, delivery_revision
    ):
        """Synchronous premium permission + reservation commit from the PREPARE
        preview (all network checks already done)."""
        if value["promotion"]:
            db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("promotions:approved:" + item, encode(preview)),
            )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) "
                "VALUES(?,'APPROVE_PREMIUM_TARIFF',?,'EXPLICIT_OWNER_PERMISSION',?)",
                (owner, item, now()),
            )
            db.execute(
                "INSERT INTO promotion_targets(item_id,disposition,actor,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(item_id) DO UPDATE SET disposition=excluded.disposition,"
                "actor=excluded.actor,updated_at=excluded.updated_at",
                (item, "include", owner, now()),
            )
            revision_row = db.execute("SELECT revision FROM promotion_policy WHERE id=1").fetchone()
            policy_revision = revision_row["revision"] if revision_row else 0
            db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    "promotions:paid_authority",
                    encode({"account_id": self.api.account.id, "revision": policy_revision, "actor": owner}),
                ),
            )
        launch = {
            "id": ident,
            "owner": owner,
            "revision": revision,
            "delivery_revision": delivery_revision,
            "at": now(),
        }
        key = launch_key(ident, revision)
        again = db.execute("SELECT id FROM outbox WHERE operation_key=?", (key,)).fetchone()
        if again:
            return again[0]
        payload = {
            **preview,
            "actor": owner,
            "automatic": False,
            "launch_id": ident,
            "launch_revision": revision,
        }
        op = self.store.enqueue(key, "premium_publish", item, payload, db, actor=owner)
        self.store.set_setting("launch:prepared:" + item, {**launch, "operation": op})
        db.execute(
            "INSERT INTO promotion_expenses VALUES(?,?,?,'RESERVED',?)",
            (key, item, preview["amount"], now()),
        )
        return op

    def guard(self, action):
        value = action["payload"]
        prepared = self.store.setting("launch:prepared:" + action["target"])
        if not prepared and not value.get("launch_id"):
            return
        if not prepared or prepared["operation"] != action["id"] or prepared["id"] != value.get("launch_id"):
            raise Conflict("Публикация не соответствует подготовленному запуску")
        draft = self.load(prepared["owner"], prepared["id"])
        config = draft["payload"]
        if (
            draft["state"] != "QUEUED"
            or draft["revision"] != prepared["revision"]
            or self.api.account.id != config["account_id"]
        ):
            raise Conflict("Подтверждение запуска устарело")
        if config["mode"] != "manual" and not getattr(self.api.account, "automation_enabled", False):
            raise Conflict("Автоматизация остановлена; публикация с автовыдачей заблокирована")
        if config["mode"] == "gifts":
            l = self.gifts.listing(action["target"])
            if (
                not l
                or not l["enabled"]
                or l["revision"] != prepared["delivery_revision"]
                or not self.gifts.runtime()
            ):
                raise Conflict("Подарочная выдача остановлена или не готова")
        elif config["mode"] != "manual":
            version, policy = self.automation.get(action["target"])
            if (
                version != prepared["delivery_revision"]
                or not policy.enabled
                or self.store.setting(f"delivery:{action['target']}:enabled") is False
            ):
                raise Conflict("Автовыдача остановлена или настройки изменились")

    async def preflight(self, action):
        if not action["payload"].get("launch_id"):
            self.guard(action)
            return
        self.guard(action)
        prepared = self.store.setting("launch:prepared:" + action["target"])
        row = self.store.rows("SELECT owner_id FROM drafts WHERE id=?", (prepared["id"],))[0]
        draft = self.load(row["owner_id"], prepared["id"])
        fresh = await self.check(row["owner_id"], draft["payload"])
        if digest(fresh) != digest(draft["payload"]["ready"]):
            raise Conflict("Готовность изменилась перед публикацией. Серверный черновик сохранён")
        self.guard(action)
