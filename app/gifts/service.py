"""Gift policy/intents share Orders, SQLite transactions and the existing Outbox."""

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from html import escape

from app.database.store import Conflict, encode, now, server_time
from app.gifts.core import (
    SEED_GIFTS,
    GiftValidationError,
    buyer_recipient,
    gift_code_from_description,
    gift_codes,
)
from app.gifts.mtproto import GiftFailure, InvalidRecipient
from app.localvpn import active_proxy
from app.security.logging import event
from app.services.order_management import deal_details, deliverable, sale
from app.services.orders import Orders
from app.services.sync_health import orders_sync_failed


def description_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class GiftService:
    def __init__(self, store, api, settings, sender=None):
        self.store, self.api, self.settings, self.sender = store, api, settings, sender

    def owner(self, owner):
        if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (owner,)):
            raise Conflict("Доступ закрыт")

    def listing(self, item_id):
        rows = self.store.rows("SELECT * FROM gift_listings WHERE item_id=?", (item_id,))
        return rows[0] if rows else None

    def job(self, order_id):
        rows = self.store.rows("SELECT * FROM gift_jobs WHERE order_id=?", (order_id,))
        if not rows:
            raise GiftFailure("GIFT_ORDER_NOT_FOUND")
        return rows[0]

    async def inspect_item(
        self, owner, item_id, *, preview_code=None, order_id=None, paid_order=None, observed_item=None
    ):
        self.owner(owner)
        s = self.settings
        if not (s.category_id and s.obtaining_type_id and s.buyer_username_field_id):
            raise GiftValidationError("Настройте точные ID категории, способа «Подарок» и поля username")
        from app.services.logical_products import LogicalProducts

        binding = LogicalProducts(self.store, self.api).listing(item_id)
        if observed_item is not None and (
            preview_code is None
            or order_id is not None
            or paid_order is not None
            or binding
            and binding["source_order_id"]
        ):
            raise Conflict("Сохранённое наблюдение допустимо только для проверки настроек")
        if binding and binding["source_order_id"]:
            if order_id != binding["source_order_id"]:
                raise Conflict("Снимок доступен только для своей оплаченной сделки")
            fresh = (
                paid_order
                if paid_order is not None
                else await self.api.call("get_delivery_deal", deal_id=order_id)
            )
            deliverable(fresh, order_id, self.api.account.id)
            item = fresh.get("item") or {}
        else:
            item = observed_item if observed_item is not None else await self.api.call("get_item", id=item_id)
        if (
            item.get("id") != item_id
            or not self.api.account.id
            or (item.get("user") or {}).get("id") != self.api.account.id
        ):
            raise GiftValidationError("Владелец лота не подтверждён")
        if (item.get("category") or {}).get("id") != s.category_id or (item.get("obtaining_type") or {}).get(
            "id"
        ) != s.obtaining_type_id:
            raise GiftValidationError("Не совпадает настроенная категория или способ «Подарок»")
        from app.services.logical_products import LogicalProducts

        if preview_code is None:
            await LogicalProducts(self.store, self.api).refresh_title(item)
        logical_code = (
            LogicalProducts(self.store, self.api).configured_gifts(item) if preview_code is None else None
        )
        if item.get("deleted_at") or item.get("status") not in {
            "DRAFT",
            "APPROVED",
            "PENDING_APPROVAL",
            "PENDING_MODERATION",
        } | ({"SOLD"} if logical_code else set()):
            raise GiftValidationError(
                "Состояние товара не разрешает настройку подарков. Обновите карточку товара"
            )
        if preview_code is not None:
            # Read-only setup preview, never used by enrollment or payment.
            code = gift_code_from_description("gift: " + preview_code, max_count=self.settings.max_gifts)
        else:
            code = logical_code
            if code is None:
                code = gift_code_from_description(item.get("description"), max_count=self.settings.max_gifts)
        # The schema is authoritative for field identity/type, never a label/substring heuristic.
        fields = []
        async for page in self.api.pages(
            "get_game_category_data_fields",
            "data_fields",
            game_category_id=s.category_id,
            obtaining_type_id=s.obtaining_type_id,
        ):
            fields.extend(page)
        chosen = [f for f in fields if f.get("id") == s.buyer_username_field_id]
        if len(chosen) != 1 or chosen[0].get("type") != "OBTAINING_DATA":
            raise GiftValidationError("Ожидаемое поле отсутствует в схеме получения заказа")
        return {
            "item": item,
            "code": code,
            "hash": description_hash(item["description"]),
            "required": chosen[0].get("required") is True,
        }

    async def refresh(self):
        gifts = await self.sender.catalog()
        balance = await self.sender.balance()
        with self.store.transaction() as db:
            identity = db.execute(
                "SELECT value FROM settings WHERE key='gifts:personal_account_id'"
            ).fetchone()
            if identity and json.loads(identity[0]) != self.sender.me.id:
                raise GiftFailure("PERSONAL_ACCOUNT_CHANGED")
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES('gifts:personal_account_id',?)",
                (encode(self.sender.me.id),),
            )
            for code, (emoji, gift_id, seed_stars) in SEED_GIFTS.items():
                gift = next((g for g in gifts if g.id == gift_id), None)
                if gift is None:
                    db.execute(
                        "INSERT INTO gift_catalog VALUES(?,?,?,?,0,?) ON CONFLICT(code) DO UPDATE SET "
                        "available=0,checked_at=excluded.checked_at",
                        (code, emoji, str(gift_id), seed_stars, now()),
                    )
                    continue
                if not isinstance(gift.stars, int) or gift.stars <= 0:
                    raise GiftFailure("CATALOG_PRICE_INVALID")
                db.execute(
                    "INSERT INTO gift_catalog VALUES(?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
                    "gift_id=excluded.gift_id,emoji=excluded.emoji,stars=excluded.stars,available=excluded.available,checked_at=excluded.checked_at",
                    (
                        code,
                        emoji,
                        str(gift_id),
                        gift.stars,
                        int(not gift.sold_out and not gift.auction),
                        now(),
                    ),
                )
            db.execute(
                "INSERT INTO settings VALUES('gifts:balance',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (encode({"stars": balance, "at": now()}),),
            )
            db.execute(
                "INSERT INTO settings VALUES('gifts:connection',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (encode({"status": "OK", "at": now()}),),
            )
        event("CHECK", f"Баланс личного Telegram: {balance} Stars; каталог обновлён")
        return balance

    async def bind(
        self,
        owner,
        item_id,
        confirmed_code,
        *,
        expected_stars=None,
        expected_revision=None,
        expected_bundle=None,
    ):
        data = await self.inspect_item(owner, item_id)
        if confirmed_code != data["code"]:
            raise GiftValidationError("Подтверждённый код не совпадает с последней строкой описания")
        await self.refresh()
        bundle = self.catalog_bundle(data["code"])
        c = {"gift_id": bundle[0]["gift_id"], "stars": sum(g["stars"] for g in bundle)}
        if expected_stars is not None and c["stars"] != expected_stars:
            raise Conflict("Цена изменилась после подтверждения; проверьте новую цену")
        if expected_bundle is not None and bundle != expected_bundle:
            raise Conflict("Цена отдельного подарка изменилась: подтвердите весь пакет заново")
        with self.store.transaction() as db:
            old = db.execute(
                "SELECT enabled,revision FROM gift_listings WHERE item_id=?", (item_id,)
            ).fetchone()
            if expected_revision is not None and (old["revision"] if old else 0) != expected_revision:
                raise Conflict("Привязка изменилась; подтвердите её заново")
            if old and old[0]:
                raise Conflict("Сначала остановите выдачу этого лота")
            db.execute(
                "INSERT INTO gift_listings(item_id,owner_id,gift_code,gift_id,approved_stars,description_sha256,"
                "category_id,obtaining_type_id,username_field_id) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(item_id) DO UPDATE SET gift_code=excluded.gift_code,gift_id=excluded.gift_id,"
                "approved_stars=excluded.approved_stars,description_sha256=excluded.description_sha256,"
                "category_id=excluded.category_id,obtaining_type_id=excluded.obtaining_type_id,"
                "username_field_id=excluded.username_field_id,revision=revision+1,enabled=0,armed_at=NULL",
                (
                    item_id,
                    owner,
                    confirmed_code,
                    c["gift_id"],
                    c["stars"],
                    data["hash"],
                    self.settings.category_id,
                    self.settings.obtaining_type_id,
                    self.settings.buyer_username_field_id,
                ),
            )
            db.execute("UPDATE gift_listings SET bundle=? WHERE item_id=?", (encode(bundle), item_id))
        return data

    def catalog_bundle(self, code):
        bundle = []
        for value in gift_codes(code):
            rows = self.store.rows("SELECT * FROM gift_catalog WHERE code=? AND available=1", (value,))
            if not rows:
                raise GiftValidationError("Подарка нет в актуальном каталоге Telegram")
            g = rows[0]
            bundle.append({"code": value, "gift_id": g["gift_id"], "stars": g["stars"]})
        return bundle

    def steps(self, order_id):
        return self.store.rows("SELECT * FROM gift_steps WHERE order_id=? ORDER BY position", (order_id,))

    def step(self, action):
        rows = self.store.rows(
            "SELECT * FROM gift_steps WHERE operation_id=? AND order_id=?", (action["id"], action["target"])
        )
        if len(rows) != 1:
            raise GiftFailure("GIFT_STEP_NOT_FOUND")
        return rows[0]

    def limits(self, item_id=None):
        v = self.store.setting("gifts:limits", {})
        local = self.store.setting("gifts:listing_options:" + item_id, {}) if item_id else {}
        return (
            min(
                int(v.get("order", self.settings.max_order_stars)),
                self.settings.max_order_stars,
                int(local.get("order", self.settings.max_order_stars)),
            ),
            min(
                int(v.get("day", self.settings.max_daily_stars)),
                self.settings.max_daily_stars,
                int(local.get("day", self.settings.max_daily_stars)),
            ),
        )

    def set_limits(self, owner, per_order, daily):
        self.owner(owner)
        if not (
            0 < per_order <= self.settings.max_order_stars
            and per_order <= daily <= self.settings.max_daily_stars
        ):
            raise GiftValidationError("Лимиты должны быть положительными и не выше защитных лимитов .env")
        self.store.set_setting("gifts:limits", {"order": per_order, "day": daily})

    def runtime(self):
        return self.runtime_reason() is None

    def runtime_reason(self):
        """Same admission gates, distinguishing owner prohibitions from free-read waits."""
        if not self.settings.payment_enabled or self.store.setting("gifts:stopped", True):
            return "AUTOMATION_STOPPED_OR_STALE"
        expected_account = self.store.setting("playerok:account_id")
        if expected_account and self.api.account.id and self.api.account.id != expected_account:
            return "AUTOMATION_STOPPED_OR_STALE"
        if not (
            getattr(self.api.account, "writes_enabled", False)
            and getattr(self.api.account, "automation_enabled", False)
        ):
            connection = getattr(self.api, "connection", None)
            if (
                connection
                and getattr(connection, "desired_writes", False)
                and getattr(connection, "desired_automation", False)
            ):
                return "CONNECTION_NOT_VERIFIED"
            return "AUTOMATION_STOPPED_OR_STALE"
        if self.store.setting("gifts:paused", False):
            return "GIFT_TEMPORARY_PAUSE"
        if self.settings.telegram_transport == "localvpn" and not (
            active_proxy.get() is not None and active_proxy.get().ready
        ):
            return "PROXY_UNAVAILABLE"
        if not self.store.setting("orders:last_complete_sync") or orders_sync_failed(self.store):
            return "GIFT_SYNC_PENDING"
        if self.store.rows(
            "SELECT 1 FROM gift_jobs WHERE state='UNCERTAIN' OR refund_state='UNCERTAIN' LIMIT 1"
        ):
            return "PAYMENT_LOCKED"
        return None

    def pause(self, owner, paused):
        """Temporary admission gate, distinct from the destructive emergency STOP."""
        self.owner(owner)
        with self.store.transaction():
            self.store.set_setting("gifts:paused", bool(paused))
            self.store.set_setting("gifts:stop_revision", self.store.setting("gifts:stop_revision", 0) + 1)

    def stop(self, owner):
        self.owner(owner)
        with self.store.transaction() as db:
            from app.services.logical_products import LogicalProducts

            logical = LogicalProducts(self.store, self.api)
            for product in db.execute(
                "SELECT id FROM logical_products WHERE owner_id=? AND handler='telegram_gifts' AND enabled=1",
                (owner,),
            ).fetchall():
                logical.stop(owner, product["id"])
            self.store.set_setting("gifts:stop_revision", self.store.setting("gifts:stop_revision", 0) + 1)
            db.execute("UPDATE gift_listings SET enabled=0,revision=revision+1")
            db.execute(
                "INSERT INTO settings VALUES('gifts:stopped','true') ON CONFLICT(key) DO UPDATE SET value='true'"
            )
        event("WARNING", "Экстренная остановка новых подарочных покупок", level=30)

    async def resume(self, owner, expected_stop_revision):
        """Explicit global permission; never arms a listing or queues a payment."""
        self.owner(owner)
        self.settings.require_configured()
        if not self.settings.payment_enabled or not getattr(self.api.account, "writes_enabled", False):
            raise GiftValidationError("Покупка подарков или запись Playerok выключены")
        if not getattr(self.api.account, "automation_enabled", False):
            raise GiftValidationError("Автоматизация выключена в настройках подключения")
        await self.refresh()
        with self.store.transaction():
            if self.store.setting("gifts:stop_revision", 0) != expected_stop_revision:
                raise Conflict("Выдача остановлена после открытия экрана. Подтвердите новое решение")
            if self.store.rows(
                "SELECT 1 FROM gift_steps WHERE state IN ('SPENDING','UNCERTAIN') LIMIT 1"
            ) or self.store.rows(
                "SELECT 1 FROM gift_jobs WHERE refund_state IN ('SPENDING','UNCERTAIN') LIMIT 1"
            ):
                raise Conflict("Сначала проверьте неизвестный результат предыдущей операции")
            # Recheck gates after the network awaits; a failed identity check must
            # not regain write authority just because the owner pressed resume.
            if not self.api.account.writes_enabled or not getattr(
                self.api.account, "automation_enabled", False
            ):
                raise Conflict("Подключение Playerok больше не подтверждено")
            self.store.set_setting("gifts:stopped", False)
        event("INFO", "Владелец разрешил новые подарочные покупки; настройки товаров не включались")

    async def arm(
        self, owner, item_id, revision, *, auto_fulfill=False, prepublication=False, logical_arm=None
    ):
        stop_revision = self.store.setting("gifts:stop_revision", 0)
        if self.store.setting("gifts:stopped", True):
            raise GiftValidationError(
                "Подарочная выдача остановлена. Явно разрешите новые покупки в настройках подарков"
            )
        if self.store.setting("gifts:paused", False):
            raise GiftValidationError("Действует временная пауза подарков. Снимите её в настройках подарков")
        data = await self.inspect_item(owner, item_id)
        self.settings.require_configured()
        disabled = []
        if not self.settings.payment_enabled:
            disabled.append("покупка подарков выключена (TG_GIFT_PAYMENTS_ENABLED)")
        if not getattr(self.api.account, "writes_enabled", False):
            disabled.append("записи Playerok выключены или аккаунт не подтверждён (PLAYEROK_WRITES_ENABLED)")
        if not getattr(self.api.account, "automation_enabled", False):
            disabled.append(
                "автоматизация выключена или аккаунт не подтверждён (PLAYEROK_AUTOMATION_ENABLED)"
            )
        if disabled:
            raise GiftValidationError("; ".join(disabled))
        await self.refresh()
        if data["item"]["status"] != ("DRAFT" if prepublication else "APPROVED"):
            raise GiftValidationError("Включать можно только опубликованный лот")
        with self.store.transaction() as db:
            if self.store.setting("gifts:stop_revision", 0) != stop_revision:
                raise Conflict("Выдача остановлена во время проверки. Подтвердите включение заново")
            row = db.execute("SELECT * FROM gift_listings WHERE item_id=?", (item_id,)).fetchone()
            if (
                not row
                or row["revision"] != revision
                or row["description_sha256"] != data["hash"]
                or row["gift_code"] != data["code"]
                or row["enabled"]
                and logical_arm is None
            ):
                raise Conflict("Привязка изменилась; подтвердите код и цену заново")
            if json.loads(row["bundle"]) != self.catalog_bundle(data["code"]):
                raise Conflict("Цена / доступность подарка изменилась: обновите привязку")
            policy = db.execute(
                "SELECT payload FROM delivery_policies WHERE item_id=?", (item_id,)
            ).fetchone()
            if (policy and json.loads(policy[0]).get("enabled")) or db.execute(
                "SELECT 1 FROM delivery_chains WHERE item_id=? AND status IN ('ACTIVE','BLOCKED')", (item_id,)
            ).fetchone():
                raise Conflict("Для лота уже действует складская/текстовая выдача или незавершённая цепочка")
            cutoff = now()
            db.execute(
                "UPDATE gift_listings SET enabled=1,auto_fulfill=?,armed_at=?,revision=revision+1 WHERE item_id=?",
                (int(auto_fulfill), cutoff, item_id),
            )
            if logical_arm is not None:
                from app.services.logical_products import LogicalProducts

                LogicalProducts(self.store, self.api).commit_arm(db, owner, item_id, logical_arm, cutoff)

    def disarm(self, owner, item_id):
        self.owner(owner)
        self.store.execute(
            "UPDATE gift_listings SET enabled=0,revision=revision+1 WHERE item_id=?", (item_id,)
        )

    def discover(self):
        if not self.runtime():
            return 0
        count = 0
        with self.store.transaction() as db:
            cursor_key = "gifts:discovery_cursor:" + str(self.api.account.id)
            cursor = self.store.setting(cursor_key, "")
            rows = db.execute(
                "SELECT o.*,l.gift_code,l.gift_id,l.revision,l.armed_at,l.owner_id FROM orders o "
                "JOIN gift_listings l ON o.item_id=l.item_id WHERE o.status='PAID' AND l.enabled=1 "
                "AND NOT EXISTS(SELECT 1 FROM gift_jobs j WHERE j.order_id=o.id) "
                "AND o.id>? ORDER BY o.id LIMIT 50",
                (cursor,),
            ).fetchall()
            # Ineligible historical rows must not pin the first page forever.
            # Persist with enqueue, so crashes replay safely through job uniqueness.
            self.store.set_setting(cursor_key, rows[-1]["id"] if rows else "")
            for row in rows:
                payload = json.loads(row["payload"])
                created, armed = server_time(payload.get("created_at")), server_time(row["armed_at"])
                if (
                    not created
                    or not armed
                    or created < armed
                    or datetime.fromisoformat(created) > datetime.now(UTC) + timedelta(minutes=5)
                ):
                    continue
                try:
                    deliverable(payload, row["id"], self.api.account.id)
                except ValueError:
                    continue
                from app.services.logical_products import LogicalProducts

                try:
                    LogicalProducts(self.store, self.api).pin_order(
                        db, row["id"], row["item_id"], "telegram_gifts"
                    )
                except Conflict:
                    continue
                if (
                    db.execute("SELECT 1 FROM reservations WHERE order_id=?", (row["id"],)).fetchone()
                    or db.execute("SELECT 1 FROM delivery_chains WHERE order_id=?", (row["id"],)).fetchone()
                    or db.execute(
                        "SELECT 1 FROM outbox WHERE target=? AND kind IN ('delivery','delivery_text','chain_message','fulfill')",
                        (row["id"],),
                    ).fetchone()
                ):
                    continue
                op = self.store.enqueue(
                    "gift:" + row["id"], "gift_send", row["id"], {}, db, actor=row["owner_id"]
                )
                db.execute(
                    "INSERT INTO gift_jobs(order_id,item_id,operation_id,listing_revision,gift_code,gift_id,reference,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        row["id"],
                        row["item_id"],
                        op,
                        row["revision"],
                        row["gift_code"],
                        row["gift_id"],
                        "PCC-" + uuid.uuid4().hex,
                        now(),
                        now(),
                    ),
                )
                bundle = json.loads(self.listing(row["item_id"])["bundle"])
                db.execute(
                    "UPDATE gift_jobs SET bundle=?,description_sha256=? WHERE order_id=?",
                    (encode(bundle), self.listing(row["item_id"])["description_sha256"], row["id"]),
                )
                for position, gift in enumerate(bundle):
                    db.execute(
                        "INSERT INTO gift_steps(order_id,position,code,gift_id,stars,operation_id,reference) VALUES(?,?,?,?,?,?,?)",
                        (
                            row["id"],
                            position,
                            gift["code"],
                            gift["gift_id"],
                            gift["stars"],
                            op if position == 0 else None,
                            "PCC-" + uuid.uuid4().hex,
                        ),
                    )
                event(
                    "ORDER",
                    "Пакет подарков поставлен в очередь: " + row["gift_code"],
                    order=row["id"],
                    operation=op,
                )
                count += 1
        return count

    def guard(self, action, *, spending=False):
        j = self.job(action["target"])
        from app.services.logical_products import LogicalProducts

        LogicalProducts(self.store, self.api).require_delivery(j["item_id"], "telegram_gifts", j["order_id"])
        l = self.listing(j["item_id"])
        lease = self.store.rows("SELECT * FROM outbox WHERE id=?", (action["id"],))[0]
        if (
            not l
            or not l["enabled"]
            or l["revision"] != j["listing_revision"]
            or l["gift_code"] != j["gift_code"]
            or l["gift_id"] != j["gift_id"]
            or l["bundle"] != j["bundle"]
            or lease["status"] != "PROCESSING"
            or lease["lease_token"] != action["lease_token"]
            or (lease["lease_until"] or 0) <= time.time()
            or j["operation_id"] != action["id"]
            or j["state"] not in ({"SPENDING"} if spending else {"PENDING", "CHECKING"})
        ):
            raise GiftFailure("AUTOMATION_STOPPED_OR_STALE")
        reason = self.runtime_reason()
        if reason is not None:
            raise GiftFailure(reason)
        local = self.store.rows("SELECT status,payload FROM orders WHERE id=?", (j["order_id"],))[0]
        if local["status"] != "PAID" or self.store.rows(
            "SELECT 1 FROM order_annotations WHERE order_id=? AND problem=1", (j["order_id"],)
        ):
            raise GiftFailure("ORDER_BLOCKED")
        deliverable(json.loads(local["payload"]), j["order_id"], self.api.account.id)
        step = self.step(action)
        if step["state"] not in ({"SPENDING"} if spending else {"PENDING", "CHECKING"}):
            raise GiftFailure("GIFT_STEP_BLOCKED")
        if self.store.rows(
            "SELECT 1 FROM gift_steps WHERE order_id=? AND position<? AND state!='SUCCEEDED'",
            (j["order_id"], step["position"]),
        ):
            raise GiftFailure("PREVIOUS_GIFT_NOT_CONFIRMED")
        return j, l

    async def fresh_order(self, job):
        from app.services.logical_products import LogicalProducts

        binding = LogicalProducts(self.store, self.api).listing(job["item_id"])
        order = (
            await self.api.call("get_delivery_deal", deal_id=job["order_id"])
            if binding and binding["source_order_id"]
            else await deal_details(
                self.api,
                job["order_id"],
                full_item=True,
                obtaining_field_id=self.listing(job["item_id"])["username_field_id"],
            )
        )
        deliverable(order, job["order_id"], self.api.account.id)
        if order["item"]["id"] != job["item_id"]:
            raise GiftFailure("ORDER_ITEM_CHANGED")
        Orders(self.store).ingest(order)
        l = self.listing(job["item_id"])
        try:
            data = await self.inspect_item(
                l["owner_id"], job["item_id"], order_id=job["order_id"], paid_order=order
            )
            from app.services.logical_products import LogicalProducts

            logical = LogicalProducts(self.store, self.api).listing(job["item_id"])
            if data["item"]["status"] != "APPROVED" and not (
                data["item"]["status"] == "SOLD" and logical and logical["state"] == "VERIFIED"
            ):
                raise GiftFailure("LISTING_NOT_PUBLISHED")
            if data["hash"] != l["description_sha256"] or data["code"] != l["gift_code"]:
                raise GiftFailure("LISTING_CHANGED")
        except ValueError:
            self.disarm(l["owner_id"], job["item_id"])
            raise GiftFailure("LISTING_CHANGED") from None
        return order

    async def execute(self, action):
        measured = {}
        stage_start = time.perf_counter()
        j, l = self.guard(action)
        step = self.step(action)
        with self.store.transaction() as db:
            if (
                db.execute(
                    "UPDATE gift_steps SET state='CHECKING' WHERE operation_id=? AND state='PENDING'",
                    (action["id"],),
                ).rowcount
                != 1
            ):
                raise GiftFailure("GIFT_ALREADY_CHECKED")
            db.execute(
                "UPDATE gift_jobs SET state='CHECKING',updated_at=? WHERE order_id=?", (now(), j["order_id"])
            )
        order = await self.fresh_order(j)
        measured["deal_check"] = time.perf_counter() - stage_start
        username = buyer_recipient(order, field_id=l["username_field_id"])
        self.store.execute(
            "UPDATE gift_jobs SET recipient_username=? WHERE order_id=? AND state='CHECKING'",
            (username, j["order_id"]),
        )
        j["recipient_username"] = username
        stage_start = time.perf_counter()
        try:
            recipient = await self.sender.resolve(username)
        except InvalidRecipient as exc:
            if not j["spent_at"]:
                await self.invalid_recipient(j, exc.code)
            raise
        if self.store.setting("gifts:personal_account_id") != self.sender.me.id or (
            j["recipient_id"] and j["recipient_id"] != recipient.user_id
        ):
            raise GiftFailure("RECIPIENT_CHANGED" if j["recipient_id"] else "PERSONAL_ACCOUNT_CHANGED")
        event("CHECK", "Получатель @[скрыто] проверен", order=j["order_id"], operation=action["id"])
        measured["recipient"] = time.perf_counter() - stage_start
        self.store.execute(
            "UPDATE gift_jobs SET recipient_username=?,recipient_id=?,recipient_access_hash=?,payer_id=?,updated_at=? WHERE order_id=? AND state='CHECKING'",
            (
                username,
                recipient.user_id,
                str(recipient.access_hash),
                self.sender.me.id,
                now(),
                j["order_id"],
            ),
        )
        bundle = json.loads(j["bundle"])
        total = sum(g["stars"] for g in bundle)
        stage_start = time.perf_counter()
        try:
            await self.sender.check_quantities(bundle[step["position"] :])
            # Validate every remaining gift before the first paid request. Forms are read-only;
            # only the current position's form is used, no new form after unknown dispatch.
            prepared = None
            for index, gift in enumerate(bundle):
                if index < step["position"]:
                    continue
                candidate = await self.sender.prepare(
                    int(gift["gift_id"]),
                    gift["stars"],
                    recipient,
                    self.steps(j["order_id"])[index]["reference"],
                )
                if index == step["position"]:
                    prepared = candidate
        except GiftFailure as exc:
            if exc.code == "PRICE_CHANGED":
                self.disarm(l["owner_id"], j["item_id"])
            raise
        measured["payment_form"] = time.perf_counter() - stage_start
        generation = self.store.rows("SELECT COUNT(*) n FROM gift_steps WHERE spent_at IS NOT NULL")[0]["n"]
        balance = await self.sender.balance()
        fresh = await self.fresh_order(j)
        if buyer_recipient(fresh, field_id=l["username_field_id"]).casefold() != username.casefold():
            raise GiftFailure("RECIPIENT_CHANGED")
        try:
            again = await self.sender.resolve(username)
        except InvalidRecipient:
            raise GiftFailure("RECIPIENT_CHANGED") from None
        if again.user_id != recipient.user_id:
            raise GiftFailure("RECIPIENT_CHANGED")
        if self.sender.me.id != j.get("payer_id", self.sender.me.id) and j.get("payer_id"):
            raise GiftFailure("PERSONAL_ACCOUNT_CHANGED")
        if self.sender.me.id != self.store.setting("gifts:personal_account_id"):
            raise GiftFailure("PERSONAL_ACCOUNT_CHANGED")
        with self.store.transaction() as db:
            self.guard(action)
            db.execute(
                "UPDATE gift_steps SET recipient_id=?,payer_id=? WHERE order_id=? AND recipient_id IS NULL",
                (recipient.user_id, self.sender.me.id, j["order_id"]),
            )
            if (
                db.execute("SELECT COUNT(*) FROM gift_steps WHERE spent_at IS NOT NULL").fetchone()[0]
                != generation
            ):
                raise GiftFailure("BALANCE_OBSERVATION_STALE")
            if db.execute(
                "SELECT 1 FROM gift_steps WHERE state IN ('SPENDING','UNCERTAIN') AND order_id!=?",
                (j["order_id"],),
            ).fetchone():
                raise GiftFailure("PAYMENT_LOCKED")
            per_order, daily = self.limits(j["item_id"])
            day = datetime.now(UTC).date().isoformat()
            # Outstanding reservations + actual/unknown dispatches today are both charged.
            actual = db.execute(
                "SELECT COALESCE(SUM(stars),0) FROM gift_steps WHERE spent_at>=?", (day,)
            ).fetchone()[0]
            reservations = db.execute("SELECT COALESCE(SUM(reserved_stars),0) FROM gift_jobs").fetchone()[0]
            current = db.execute(
                "SELECT reserved_stars,reserved_day FROM gift_jobs WHERE order_id=?", (j["order_id"],)
            ).fetchone()
            remaining = db.execute(
                "SELECT COALESCE(SUM(stars),0) FROM gift_steps WHERE order_id=? AND spent_at IS NULL",
                (j["order_id"],),
            ).fetchone()[0]
            reserved_other = reservations - current["reserved_stars"]
            if total > per_order:
                raise GiftFailure("BUDGET_EXCEEDED")
            if actual + reserved_other + remaining > daily:
                raise GiftFailure("DAILY_BUDGET_EXHAUSTED")
            if balance < reserved_other + remaining:
                raise GiftFailure("BALANCE_TOO_LOW")
            if db.execute("SELECT 1 FROM gift_steps WHERE form_id=?", (str(prepared.form_id),)).fetchone():
                raise GiftFailure("PAYMENT_FORM_ALREADY_USED")
            # One atomic fence before paid intent; an expired free-check lease
            # can never wake up and dispatch after recovery claimed its retry.
            self.store.mark_started(action)
            db.execute(
                "UPDATE gift_jobs SET reserved_day=?,reserved_stars=?,state='SPENDING',spent_at=COALESCE(spent_at,?),form_id=?,updated_at=? WHERE order_id=?",
                (day, remaining - prepared.stars, now(), str(prepared.form_id), now(), j["order_id"]),
            )
            db.execute(
                "UPDATE gift_steps SET state='SPENDING',receipt_mode='MESSAGE_ID',recipient_id=?,payer_id=?,form_id=?,spent_at=? WHERE operation_id=? AND state='CHECKING'",
                (recipient.user_id, self.sender.me.id, str(prepared.form_id), now(), action["id"]),
            )
        event(
            "CHECK",
            f"Стоимость пакета: {total} Stars; баланс и лимиты проверены",
            order=j["order_id"],
            operation=action["id"],
        )
        self.guard(action, spending=True)
        event(
            "GIFT",
            f"{step['code']} {step['position'] + 1}/{len(bundle)} — отправка",
            order=j["order_id"],
            operation=action["id"],
            position=step["position"] + 1,
        )
        stage_start = time.perf_counter()
        result = await self.sender.pay(prepared)
        measured["payment_rpc"] = time.perf_counter() - stage_start
        evidence = {**self.job(j["order_id"]), **self.step(action)}
        acknowledgement = self.sender.acknowledgement(evidence, result)
        # Save the external RPC correlation before the independent network read.
        # Losing this write is conservative uncertainty, never permission to pay.
        with self.store.transaction() as db:
            claimed = db.execute(
                "SELECT 1 FROM gift_steps WHERE payer_id=? AND recipient_id=? "
                "AND (ack_message_id=? OR message_id=?) AND operation_id!=?",
                (
                    evidence["payer_id"],
                    evidence["recipient_id"],
                    acknowledgement,
                    acknowledgement,
                    action["id"],
                ),
            ).fetchone()
            if claimed:
                raise GiftFailure("DELIVERY_RECEIPT_ALREADY_USED")
            if (
                db.execute(
                    "UPDATE gift_steps SET ack_message_id=? WHERE operation_id=? AND state='SPENDING' "
                    "AND ack_message_id IS NULL AND receipt_mode='MESSAGE_ID'",
                    (acknowledgement, action["id"]),
                ).rowcount
                != 1
            ):
                raise GiftFailure("DELIVERY_CORRELATION_UNKNOWN")
        evidence["ack_message_id"] = acknowledgement
        stage_start = time.perf_counter()
        receipt = await self.sender.receipt(evidence, result)
        measured["receipt_read"] = time.perf_counter() - stage_start
        return {
            "gift_receipt": receipt,
            "stage_durations": {key: round(value, 6) for key, value in measured.items()},
        }

    def continue_bundle(self, order_id, db):
        j = self.job(order_id)
        steps = self.steps(order_id)
        pending = next((g for g in steps if g["state"] != "SUCCEEDED"), None)
        if pending is None:
            receipts = [json.loads(g["receipt"]) for g in steps]
            db.execute(
                "UPDATE gift_jobs SET state='SUCCEEDED',receipt=?,stars=?,reserved_stars=0,updated_at=? WHERE order_id=?",
                (
                    encode(receipts[0] if len(receipts) == 1 else {"gifts": receipts}),
                    sum(g["stars"] for g in steps),
                    now(),
                    order_id,
                ),
            )
            return
        if pending["state"] != "PENDING" or pending["operation_id"]:
            return
        op = self.store.enqueue(
            f"gift:{order_id}:{pending['position']}",
            "gift_send",
            order_id,
            {"position": pending["position"]},
            db,
            actor=self.listing(j["item_id"])["owner_id"],
        )
        db.execute(
            "UPDATE gift_steps SET operation_id=? WHERE order_id=? AND position=?",
            (op, order_id, pending["position"]),
        )
        db.execute(
            "UPDATE gift_jobs SET operation_id=?,state='PENDING',updated_at=? WHERE order_id=?",
            (op, now(), order_id),
        )

    async def invalid_recipient(self, job, code):
        # Only two definitive resolveUsername RPC errors reach here, never timeout/FloodWait.
        self.store.execute(
            "UPDATE gift_jobs SET state='INVALID_RECIPIENT',error_code=?,refund_state='MANUAL',updated_at=? WHERE order_id=? AND state='CHECKING'",
            (code, now(), job["order_id"]),
        )
        order = await deal_details(self.api, job["order_id"])
        deliverable(order, job["order_id"], self.api.account.id)
        listing = self.listing(job["item_id"])
        if (order.get("item") or {}).get("id") != job["item_id"] or buyer_recipient(
            order, field_id=listing["username_field_id"]
        ).casefold() != (job.get("recipient_username") or "").casefold():
            raise GiftFailure("REFUND_RECIPIENT_CHANGED")
        Orders(self.store).ingest(order)
        # Native updateDeal is confirmed in the official seller UI, with the adapter's
        # conservative status/ownership/non-system guard and independent readback.
        capability = getattr(self.api, "gift_refund_capability", None)
        from app.services.logical_products import refund_allowed

        supported = (
            await capability(order)
            if capability
            and self.settings.refund_enabled
            and refund_allowed(self.store, job["item_id"], job["order_id"])
            else False
        )
        if supported is True:
            with self.store.transaction() as db:
                op = self.store.enqueue(
                    "gift-refund:" + job["order_id"],
                    "gift_refund",
                    job["order_id"],
                    {},
                    db,
                    actor=self.listing(job["item_id"])["owner_id"],
                )
                db.execute(
                    "UPDATE gift_jobs SET refund_state='QUEUED',refund_operation_id=? WHERE order_id=? AND state='INVALID_RECIPIENT' AND spent_at IS NULL",
                    (op, job["order_id"]),
                )

    def queue_notice(self, db, job, outcome):
        self.store.enqueue(
            "gift-notice:" + job["order_id"] + ":" + outcome,
            "gift_notice",
            job["order_id"],
            {"outcome": outcome},
            db,
            actor=self.listing(job["item_id"])["owner_id"],
        )

    async def reconcile(self, owner, order_id):
        self.owner(owner)
        job = self.job(order_id)
        unknown = [g for g in self.steps(order_id) if g["state"] == "UNCERTAIN" and g["spent_at"]]
        if job["state"] != "UNCERTAIN" or len(unknown) != 1:
            raise GiftValidationError("Сверка доступна только для неизвестного результата отправки")
        step = unknown[0]
        receipt = await self.sender.receipt({**job, **step})
        with self.store.transaction() as db:
            if (
                db.execute(
                    "UPDATE gift_steps SET state='SUCCEEDED',receipt=?,message_id=? WHERE order_id=? AND position=? AND state='UNCERTAIN'",
                    (encode(receipt), receipt["message_id"], order_id, step["position"]),
                ).rowcount
                != 1
            ):
                raise Conflict("Состояние подарка изменилось")
            self.continue_bundle(order_id, db)
            if self.job(order_id)["state"] == "SUCCEEDED":
                self.queue_notice(db, job, "DELIVERED")
        event("SUCCESS", "Доставка подтверждена сверкой", order=order_id, position=step["position"] + 1)
        return receipt

    async def recover_resource_preflights(self):
        """Previous releases' proven free failures; same operation, never unknowns."""
        if not self.runtime():
            return 0
        from app.gifts.outcomes import unstarted_preflight
        from app.playerok.adapter import APIError
        from app.services.automation import future_order
        from app.services.logical_products import LogicalProducts

        recovered = 0
        candidates = self.store.rows(
            "SELECT o.id,o.target FROM outbox o JOIN gift_jobs j ON j.operation_id=o.id "
            "JOIN gift_listings l ON l.item_id=j.item_id WHERE o.kind='gift_send' "
            "AND o.status='PERMANENT_FAILED' AND o.retry_at<=? AND j.state='BLOCKED' "
            "AND l.enabled=1 AND l.revision=j.listing_revision AND l.bundle=j.bundle "
            "AND j.refund_state='NONE' AND o.error_code IN "
            "('BALANCE_TOO_LOW','GIFT_UNAVAILABLE','GIFT_QUANTITY_UNAVAILABLE','CATALOG_UNAVAILABLE','BALANCE_UNAVAILABLE','BUDGET_EXCEEDED') "
            "ORDER BY o.retry_at,o.created_at LIMIT 10",
            (time.time(),),
        )
        for candidate in candidates:
            with self.store.transaction() as db:
                if not unstarted_preflight(db, candidate, blocked=True):
                    continue
                self.store.execute(
                    "UPDATE outbox SET retry_at=? WHERE id=? AND status='PERMANENT_FAILED'",
                    (time.time() + 30, candidate["id"]),
                )
                operation = self.store.rows("SELECT * FROM outbox WHERE id=?", (candidate["id"],))
                job = self.job(candidate["target"])
                listing = self.listing(job["item_id"])
                local = self.store.rows(
                    "SELECT status,item_id,payload FROM orders WHERE id=?", (job["order_id"],)
                )
            try:
                logical = LogicalProducts(self.store, self.api)
                product = logical.require_delivery(job["item_id"], "telegram_gifts", job["order_id"])
                if (
                    not product
                    or not local
                    or local[0]["status"] != "PAID"
                    or local[0]["item_id"] != job["item_id"]
                ):
                    continue
                if (
                    operation[0]["error_code"] == "BUDGET_EXCEEDED"
                    and sum(g["stars"] for g in json.loads(job["bundle"])) > self.limits(job["item_id"])[0]
                ):
                    continue
                order = await deal_details(
                    self.api, job["order_id"], full_item=True, obtaining_field_id=listing["username_field_id"]
                )
                deliverable(order, job["order_id"], self.api.account.id)
                if (
                    (order.get("item") or {}).get("id") != job["item_id"]
                    or server_time(order.get("created_at"))
                    != server_time(json.loads(local[0]["payload"]).get("created_at"))
                    or not future_order(order.get("created_at"), product["armed_at"])
                    or not future_order(order.get("created_at"), listing["armed_at"])
                ):
                    continue
                username = buyer_recipient(order, field_id=listing["username_field_id"])
                if job["recipient_username"] and username.casefold() != job["recipient_username"].casefold():
                    continue
                item = (
                    (order.get("item") or {})
                    if product["source_order_id"]
                    else await self.api.call("get_item", id=job["item_id"])
                )
                logical.verify_listing(product, item)
            except (APIError, ValueError):
                continue
            with self.store.transaction() as db:
                if (
                    not self.runtime()
                    or self.store.rows("SELECT * FROM outbox WHERE id=?", (candidate["id"],)) != operation
                    or self.job(job["order_id"]) != job
                    or self.listing(job["item_id"]) != listing
                    or self.store.rows(
                        "SELECT status,item_id,payload FROM orders WHERE id=?", (job["order_id"],)
                    )
                    != local
                    or not unstarted_preflight(db, candidate, blocked=True)
                ):
                    continue
                try:
                    logical.require_delivery(job["item_id"], "telegram_gifts", job["order_id"])
                except Conflict:
                    continue
                if db.execute(
                    "SELECT 1 FROM reservations WHERE order_id=? UNION ALL "
                    "SELECT 1 FROM delivery_chains WHERE order_id=? UNION ALL "
                    "SELECT 1 FROM outbox WHERE target=? AND kind IN ('delivery','delivery_text','chain_message','fulfill') LIMIT 1",
                    (job["order_id"], job["order_id"], job["order_id"]),
                ).fetchone():
                    continue
                if self.store.rows(
                    "SELECT 1 FROM order_annotations WHERE order_id=? AND problem=1", (job["order_id"],)
                ):
                    continue
                changed = db.execute(
                    "UPDATE outbox SET status='PENDING',error_code=NULL,lease_token=NULL,lease_until=NULL,retry_at=0 WHERE id=? AND status='PERMANENT_FAILED'",
                    (candidate["id"],),
                ).rowcount
                if not changed:
                    continue
                db.execute(
                    "UPDATE gift_steps SET state='PENDING',error_code=NULL WHERE operation_id=? AND state='BLOCKED'",
                    (candidate["id"],),
                )
                db.execute(
                    "UPDATE gift_jobs SET state='PENDING',error_code=NULL,updated_at=? WHERE operation_id=? AND state='BLOCKED'",
                    (now(), candidate["id"]),
                )
                self.store.audit(
                    listing["owner_id"], "GIFT_FREE_PREFLIGHT_RECOVERED", job["order_id"], candidate["id"]
                )
                recovered += 1
        return recovered

    def retry_preflight(self, owner, order_id):
        self.owner(owner)
        with self.store.transaction() as db:
            j = self.job(order_id)
            l = self.listing(j["item_id"])
            candidates = [g for g in self.steps(order_id) if g["state"] != "SUCCEEDED"]
            step = candidates[0] if candidates else None
            op = (
                db.execute("SELECT status FROM outbox WHERE id=?", (step["operation_id"],)).fetchone()
                if step
                else None
            )
            if (
                j["state"] != "BLOCKED"
                or not step
                or step["state"] != "BLOCKED"
                or step["spent_at"]
                or step["form_id"]
                or j["refund_state"] != "NONE"
                or not op
                or op[0] not in {"PERMANENT_FAILED", "UNCERTAIN"}
                or not self.runtime()
                or not l["enabled"]
                or j["bundle"] != l["bundle"]
                or j["description_sha256"] != l["description_sha256"]
            ):
                raise Conflict("Повтор безопасной проверки недоступен; неизвестные оплаты не повторяются")
            operation = self.store.enqueue(
                "gift-recheck:" + order_id + ":" + uuid.uuid4().hex,
                "gift_send",
                order_id,
                {"position": step["position"]},
                db,
                actor=owner,
            )
            db.execute(
                "UPDATE gift_steps SET state='PENDING',operation_id=?,error_code=NULL WHERE order_id=? AND position=?",
                (operation, order_id, step["position"]),
            )
            db.execute(
                "UPDATE gift_jobs SET state='PENDING',operation_id=?,listing_revision=?,error_code=NULL,updated_at=? WHERE order_id=?",
                (operation, l["revision"], now(), order_id),
            )

    async def reconcile_refund(self, owner, order_id):
        self.owner(owner)
        j = self.job(order_id)
        if j["refund_state"] not in {"UNCERTAIN", "MANUAL", "FAILED"} or j["spent_at"]:
            raise GiftValidationError("Сверка возврата недоступна")
        order = await self.api.call("get_deal", deal_id=order_id)
        sale(order, order_id, self.api.account.id)
        if ((order.get("item") or {}).get("user") or {}).get("id") != self.api.account.id or order.get(
            "status"
        ) != "ROLLED_BACK":
            raise GiftFailure("REFUND_NOT_CONFIRMED")
        with self.store.transaction() as db:
            db.execute(
                "UPDATE gift_jobs SET refund_state='CONFIRMED',updated_at=? WHERE order_id=? AND spent_at IS NULL",
                (now(), order_id),
            )
            self.queue_notice(db, j, "REFUNDED")
        Orders(self.store).ingest(order)

    def status_text(self, item_id):
        s = self
        l = s.listing(item_id)
        if not l:
            return "🎁 Подарочная выдача не настроена"
        reasons = []
        from app.services.logical_products import LogicalProducts

        try:
            LogicalProducts(self.store, self.api).require_delivery(item_id, "telegram_gifts")
        except Conflict as error:
            reasons.append(str(error))
        if not l["enabled"]:
            reasons.append("лот остановлен владельцем")
        if not s.settings.payment_enabled:
            reasons.append("платные отправки запрещены конфигурацией")
        if not getattr(s.api.account, "writes_enabled", False):
            reasons.append("записи Playerok выключены")
        if not getattr(s.api.account, "automation_enabled", False):
            reasons.append("глобальная автоматика выключена")
        if self.store.setting("gifts:stopped", True):
            reasons.append("общая остановка подарков")
        if self.store.setting("gifts:paused", False):
            reasons.append("временная пауза подарочных покупок; настройки товаров сохранены")
        if not self.store.setting("orders:last_complete_sync"):
            reasons.append("сверка заказов не завершена")
        if orders_sync_failed(self.store):
            reasons.append("ошибка синхронизации")
        if self.store.rows("SELECT 1 FROM gift_jobs WHERE state IN ('SPENDING','UNCERTAIN')"):
            reasons.append("предыдущий платёж требует сверки")
        connection = self.store.setting("gifts:connection", {}).get("status", "NOT_CHECKED")
        if connection != "OK":
            reasons.append("MTProto не проверен")
        status = (
            "Остановлено"
            if not l["enabled"] or self.store.setting("gifts:stopped", True)
            else "Не готово"
            if reasons
            else "Работает"
        )
        return (
            f"🎁 Подарочная выдача: {status}\nРежим: {escape(l['gift_code'])}, согласовано {l['approved_stars']} ⭐\n"
            + "Причины: "
            + ("; ".join(reasons) or "нет; окончательная проверка перед каждой покупкой")
            + "\nСкладская выдача этого заказа исключена."
        )
