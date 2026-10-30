"""Gift policy/intents share Orders, SQLite transactions and the existing Outbox."""

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from html import escape

from app.database.store import Conflict, encode, now, server_time
from app.gifts.core import SEED_GIFTS, GiftValidationError, buyer_recipient, gift_code_from_description
from app.gifts.mtproto import GiftFailure, InvalidRecipient
from app.services.order_management import deliverable, sale
from app.services.orders import Orders


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

    async def inspect_item(self, owner, item_id):
        self.owner(owner)
        s = self.settings
        if not (s.category_id and s.obtaining_type_id and s.buyer_username_field_id):
            raise GiftValidationError("Настройте точные ID категории, способа «Подарок» и поля username")
        item = await self.api.call("get_item", id=item_id)
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
        if item.get("deleted_at") or item.get("status") not in {"DRAFT", "APPROVED"}:
            raise GiftValidationError("Лот удалён или недоступен для настройки")
        code = gift_code_from_description(item.get("description"))
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
            for code, (emoji, gift_id, _) in SEED_GIFTS.items():
                gift = next((g for g in gifts if g.id == gift_id), None)
                if gift is None:
                    db.execute("UPDATE gift_catalog SET available=0,checked_at=? WHERE code=?", (now(), code))
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
        return balance

    async def bind(self, owner, item_id, confirmed_code, *, expected_stars=None, expected_revision=None):
        data = await self.inspect_item(owner, item_id)
        if confirmed_code != data["code"]:
            raise GiftValidationError("Подтверждённый код не совпадает с последней строкой описания")
        await self.refresh()
        catalog = self.store.rows(
            "SELECT * FROM gift_catalog WHERE code=? AND available=1", (confirmed_code,)
        )
        if not catalog:
            raise GiftValidationError("Подарка нет в актуальном каталоге Telegram")
        c = catalog[0]
        if expected_stars is not None and c["stars"] != expected_stars:
            raise Conflict("Цена изменилась после подтверждения; проверьте новую цену")
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
        return data

    def limits(self):
        v = self.store.setting("gifts:limits", {})
        return (
            min(int(v.get("order", self.settings.max_order_stars)), self.settings.max_order_stars),
            min(int(v.get("day", self.settings.max_daily_stars)), self.settings.max_daily_stars),
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
        return (
            self.settings.payment_enabled
            and getattr(self.api.account, "writes_enabled", False)
            and getattr(self.api.account, "automation_enabled", False)
            and not self.store.setting("gifts:stopped", True)
            and bool(self.store.setting("orders:last_complete_sync"))
            and self.store.setting("sync:health", {}).get("status") != "ERROR"
            and not self.store.rows(
                "SELECT 1 FROM gift_jobs WHERE state='UNCERTAIN' OR refund_state='UNCERTAIN' LIMIT 1"
            )
        )

    def stop(self, owner):
        self.owner(owner)
        with self.store.transaction() as db:
            db.execute("UPDATE gift_listings SET enabled=0,revision=revision+1")
            db.execute(
                "INSERT INTO settings VALUES('gifts:stopped','true') ON CONFLICT(key) DO UPDATE SET value='true'"
            )

    async def arm(self, owner, item_id, revision, *, auto_fulfill=False):
        data = await self.inspect_item(owner, item_id)
        self.settings.require_configured()
        if (
            not self.settings.payment_enabled
            or not getattr(self.api.account, "writes_enabled", False)
            or not getattr(self.api.account, "automation_enabled", False)
        ):
            raise GiftValidationError("Глобальные платные отправки / записи / автоматика выключены")
        await self.refresh()
        if data["item"]["status"] != "APPROVED":
            raise GiftValidationError("Включать можно только опубликованный лот")
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM gift_listings WHERE item_id=?", (item_id,)).fetchone()
            if (
                not row
                or row["revision"] != revision
                or row["description_sha256"] != data["hash"]
                or row["gift_code"] != data["code"]
                or row["enabled"]
            ):
                raise Conflict("Привязка изменилась; подтвердите код и цену заново")
            live = db.execute("SELECT * FROM gift_catalog WHERE code=?", (row["gift_code"],)).fetchone()
            if not live or not live["available"] or live["stars"] != row["approved_stars"]:
                raise Conflict("Цена / доступность подарка изменилась: обновите привязку")
            policy = db.execute(
                "SELECT payload FROM delivery_policies WHERE item_id=?", (item_id,)
            ).fetchone()
            if (policy and json.loads(policy[0]).get("enabled")) or db.execute(
                "SELECT 1 FROM delivery_chains WHERE item_id=? AND status IN ('ACTIVE','BLOCKED')", (item_id,)
            ).fetchone():
                raise Conflict("Для лота уже действует складская/текстовая выдача или незавершённая цепочка")
            db.execute(
                "UPDATE gift_listings SET enabled=1,auto_fulfill=?,armed_at=?,revision=revision+1 WHERE item_id=?",
                (int(auto_fulfill), now(), item_id),
            )
            db.execute(
                "INSERT INTO settings VALUES('gifts:stopped','false') ON CONFLICT(key) DO UPDATE SET value='false'"
            )

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
            rows = db.execute(
                "SELECT o.*,l.gift_code,l.gift_id,l.revision,l.armed_at,l.owner_id FROM orders o "
                "JOIN gift_listings l ON o.item_id=l.item_id WHERE o.status='PAID' AND l.enabled=1 "
                "AND NOT EXISTS(SELECT 1 FROM gift_jobs j WHERE j.order_id=o.id) LIMIT 50"
            ).fetchall()
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
                count += 1
        return count

    def guard(self, action, *, spending=False):
        j = self.job(action["target"])
        l = self.listing(j["item_id"])
        lease = self.store.rows("SELECT * FROM outbox WHERE id=?", (action["id"],))[0]
        if (
            not self.runtime()
            or not l
            or not l["enabled"]
            or l["revision"] != j["listing_revision"]
            or l["gift_code"] != j["gift_code"]
            or l["gift_id"] != j["gift_id"]
            or SEED_GIFTS.get(l["gift_code"], ("", 0, 0))[1] != int(l["gift_id"])
            or lease["status"] != "PROCESSING"
            or lease["lease_token"] != action["lease_token"]
            or (lease["lease_until"] or 0) <= time.time()
            or j["operation_id"] != action["id"]
            or j["state"] not in ({"SPENDING"} if spending else {"PENDING", "CHECKING"})
        ):
            raise GiftFailure("AUTOMATION_STOPPED_OR_STALE")
        local = self.store.rows("SELECT status,payload FROM orders WHERE id=?", (j["order_id"],))[0]
        if local["status"] != "PAID" or self.store.rows(
            "SELECT 1 FROM order_annotations WHERE order_id=? AND problem=1", (j["order_id"],)
        ):
            raise GiftFailure("ORDER_BLOCKED")
        deliverable(json.loads(local["payload"]), j["order_id"], self.api.account.id)
        return j, l

    async def fresh_order(self, job):
        order = await self.api.call("get_deal", deal_id=job["order_id"])
        deliverable(order, job["order_id"], self.api.account.id)
        if order["item"]["id"] != job["item_id"]:
            raise GiftFailure("ORDER_ITEM_CHANGED")
        Orders(self.store).ingest(order)
        l = self.listing(job["item_id"])
        try:
            data = await self.inspect_item(l["owner_id"], job["item_id"])
            if data["hash"] != l["description_sha256"] or data["code"] != l["gift_code"]:
                raise GiftFailure("LISTING_CHANGED")
        except ValueError:
            self.disarm(l["owner_id"], job["item_id"])
            raise GiftFailure("LISTING_CHANGED") from None
        return order

    async def execute(self, action):
        j, l = self.guard(action)
        if (
            self.store.execute(
                "UPDATE gift_jobs SET state='CHECKING',updated_at=? WHERE order_id=? AND state='PENDING'",
                (now(), j["order_id"]),
            ).rowcount
            != 1
        ):
            raise GiftFailure("GIFT_ALREADY_CHECKED")
        order = await self.fresh_order(j)
        username = buyer_recipient(order, field_id=l["username_field_id"])
        try:
            recipient = await self.sender.resolve(username)
        except InvalidRecipient as exc:
            await self.invalid_recipient(j, exc.code)
            raise
        if self.store.setting("gifts:personal_account_id") != self.sender.me.id:
            raise GiftFailure("PERSONAL_ACCOUNT_CHANGED")
        self.store.execute(
            "UPDATE gift_jobs SET recipient_username=?,recipient_id=?,recipient_access_hash=?,updated_at=? WHERE order_id=? AND state='CHECKING'",
            (username, recipient.user_id, str(recipient.access_hash), now(), j["order_id"]),
        )
        self.store.execute(
            "UPDATE gift_jobs SET payer_id=? WHERE order_id=? AND state='CHECKING'",
            (self.sender.me.id, j["order_id"]),
        )
        try:
            prepared = await self.sender.prepare(
                int(j["gift_id"]), l["approved_stars"], recipient, j["reference"]
            )
        except GiftFailure as exc:
            if exc.code in {"PRICE_CHANGED", "GIFT_UNAVAILABLE"}:
                self.disarm(l["owner_id"], j["item_id"])
            raise
        fresh = await self.fresh_order(j)
        if buyer_recipient(fresh, field_id=l["username_field_id"]).casefold() != username.casefold():
            raise GiftFailure("RECIPIENT_CHANGED")
        try:
            resolved_again = await self.sender.resolve(username)
        except InvalidRecipient:
            # It existed in this attempt; a changed binding is not evidence for auto refund.
            raise GiftFailure("RECIPIENT_CHANGED") from None
        if resolved_again.user_id != recipient.user_id:
            raise GiftFailure("RECIPIENT_CHANGED")
        with self.store.transaction() as db:
            self.guard(action)
            if db.execute(
                "SELECT 1 FROM gift_jobs WHERE state IN ('SPENDING','UNCERTAIN') AND order_id!=?",
                (j["order_id"],),
            ).fetchone():
                raise GiftFailure("PAYMENT_LOCKED")
            per_order, daily = self.limits()
            spent = db.execute(
                "SELECT COALESCE(SUM(stars),0) FROM gift_jobs WHERE spent_at>=?",
                (datetime.now(UTC).date().isoformat(),),
            ).fetchone()[0]
            if prepared.stars > per_order or spent + prepared.stars > daily:
                raise GiftFailure("BUDGET_EXCEEDED")
            if db.execute("SELECT 1 FROM gift_jobs WHERE form_id=?", (str(prepared.form_id),)).fetchone():
                raise GiftFailure("PAYMENT_FORM_ALREADY_USED")
            db.execute(
                "UPDATE gift_jobs SET state='SPENDING',stars=?,form_id=?,spent_at=?,updated_at=? WHERE order_id=? AND state='CHECKING'",
                (prepared.stars, str(prepared.form_id), now(), now(), j["order_id"]),
            )
        # No await between this final permission check and entering the single payment RPC.
        self.guard(action, spending=True)
        result = await self.sender.pay(prepared)
        receipt = await self.sender.receipt(self.job(j["order_id"]), result)
        return {"gift_receipt": receipt}

    async def invalid_recipient(self, job, code):
        # Only two definitive resolveUsername RPC errors reach here, never timeout/FloodWait.
        self.store.execute(
            "UPDATE gift_jobs SET state='INVALID_RECIPIENT',error_code=?,refund_state='MANUAL',updated_at=? WHERE order_id=? AND state='CHECKING'",
            (code, now(), job["order_id"]),
        )
        order = await self.api.call("get_deal", deal_id=job["order_id"])
        deliverable(order, job["order_id"], self.api.account.id)
        Orders(self.store).ingest(order)
        # Native updateDeal is confirmed in the official seller UI, with the adapter's
        # conservative status/ownership/non-system guard and independent readback.
        capability = getattr(self.api, "gift_refund_capability", None)
        supported = await capability(order) if capability and self.settings.refund_enabled else False
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
        if job["state"] != "UNCERTAIN" or not job["spent_at"]:
            raise GiftValidationError("Сверка доступна только для неизвестного результата отправки")
        receipt = await self.sender.receipt(job)
        with self.store.transaction() as db:
            if (
                db.execute(
                    "UPDATE gift_jobs SET state='SUCCEEDED',receipt=?,message_id=?,updated_at=? WHERE order_id=? AND state='UNCERTAIN' AND refund_state='NONE'",
                    (encode(receipt), receipt["message_id"], now(), order_id),
                ).rowcount
                != 1
            ):
                raise Conflict("Состояние подарка изменилось")
            # Keep the immutable outbox UNKNOWN record; this is a separate confirmed read receipt.
            self.queue_notice(db, job, "DELIVERED")
        return receipt

    def retry_preflight(self, owner, order_id):
        """Explicit owner retry of a proved pre-payment failure; never resets an unknown payment."""
        self.owner(owner)
        with self.store.transaction() as db:
            j = self.job(order_id)
            l = self.listing(j["item_id"])
            op = db.execute("SELECT status FROM outbox WHERE id=?", (j["operation_id"],)).fetchone()
            if (
                j["state"] != "BLOCKED"
                or j["spent_at"]
                or j["form_id"]
                or j["refund_state"] != "NONE"
                or not op
                or op[0] != "PERMANENT_FAILED"
                or not self.runtime()
                or not l["enabled"]
            ):
                raise Conflict("Повтор безопасной проверки недоступен; неизвестные оплаты не повторяются")
            operation = self.store.enqueue(
                "gift-recheck:" + order_id + ":" + uuid.uuid4().hex,
                "gift_send",
                order_id,
                {},
                db,
                actor=owner,
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
        if not self.store.setting("orders:last_complete_sync"):
            reasons.append("сверка заказов не завершена")
        if self.store.setting("sync:health", {}).get("status") == "ERROR":
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
