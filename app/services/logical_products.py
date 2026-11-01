"""Identity and verified item projections; execution stays in Gifts/DeliveryChains."""

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from dataclasses import asdict

from app.database.store import Conflict, encode, now
from app.gifts.core import gift_code_from_description
from app.services.automation import Automation, DeliveryPolicy, future_order, message_text
from app.services.products import field_baseline, listing_text

PROBLEMS = {
    "CATALOG_MARKER_INVALID": "В описании обнаружен некорректный код. Проверьте связанные объявления.",
    "DUPLICATE_ACTIVE_MARKER": "Найдено несколько активных объявлений с одним кодом. Разрешите конфликт.",
    "RELIST_CONFIRMATION_REQUIRED": "Новое объявление не прошло проверку. Проверьте его параметры и настройки.",
}

HANDLERS = frozenset({"telegram_gifts", "unique_stock", "fixed_text", "manual"})


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def marker(description):
    if not isinstance(description, str):
        raise Conflict("Playerok не вернул описание товара")
    lines = description.splitlines()
    candidates = [line.strip() for line in lines if re.search(r"#\d", line)]
    if not candidates:
        return None
    nonempty = [line.strip() for line in lines if line.strip()]
    if len(candidates) != 1 or not re.fullmatch(r"#[0-9]{7}", candidates[0]) or nonempty[-1] != candidates[0]:
        raise ValueError("В описании нужен один постоянный код из семи цифр в последней строке")
    return candidates[0]


def marked_description(description, code):
    if not re.fullmatch(r"#[0-9]{7}", code):
        raise ValueError("Некорректный постоянный код")
    existing = marker(description)
    if existing and existing != code:
        raise Conflict("В описании уже указан код другого товара")
    return description.rstrip() if existing else description.rstrip() + "\n" + code


def critical(item):
    account = (item.get("user") or {}).get("id")
    category = (item.get("category") or {}).get("id")
    obtaining = (item.get("obtaining_type") or {}).get("id")
    if not account or not category or not obtaining or item.get("price") is None or not item.get("name"):
        raise ValueError("Не подтверждены владелец, категория, способ получения, название или цена")
    return {
        "account": account,
        "category": category,
        "obtaining": obtaining,
        "name": item["name"],
        "price": str(item["price"]),
        "fields": field_baseline(item),
    }


def configuration(handler, config, *, max_count=5):
    if handler not in HANDLERS or not isinstance(config, dict):
        raise ValueError("Выберите поддерживаемый способ выдачи")
    result = dict(config)
    if handler == "telegram_gifts":
        result["code"] = gift_code_from_description("gift: " + result.get("code", ""), max_count=max_count)
    if handler == "fixed_text":
        result["text"] = message_text(result.get("text"), required=True)
    if handler != "manual":
        result["after"] = message_text(result.get("after", ""))
    return result


class LogicalProducts:
    def __init__(self, store, api=None):
        self.store, self.api = store, api

    def authority(self, owner, account=None, *, writes=False):
        if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (owner,)):
            raise Conflict("Доступ закрыт")
        current = self.api.account.id if self.api else self.store.setting("playerok:account_id")
        if (
            not current
            or current != self.store.setting("playerok:account_id")
            or account
            and account != current
        ):
            raise Conflict("Подключённый аккаунт Playerok не подтверждён или изменился")
        if writes and not self.api.account.writes_enabled:
            raise Conflict("Записи Playerok выключены; привязка описания не отправлена")
        return current

    def product(self, ident, owner=None):
        rows = self.store.rows("SELECT * FROM logical_products WHERE id=?", (ident,))
        if not rows or owner is not None and rows[0]["owner_id"] != owner:
            raise Conflict("Логический товар не найден")
        return rows[0]

    def listing(self, item_id):
        rows = self.store.rows(
            "SELECT p.*,l.item_id,l.config_revision,l.description_sha256,l.server_status,l.verified_at,l.operation_id,l.source_order_id "
            "FROM logical_listings l JOIN logical_products p ON p.id=l.product_id WHERE l.item_id=?",
            (item_id,),
        )
        return rows[0] if rows else None

    def gift_service(self):
        from app.gifts.mtproto import PersonalGiftSender
        from app.gifts.service import GiftService
        from app.gifts.settings import GiftSettings

        settings = getattr(self.api, "gift_settings", None) or GiftSettings.load()
        if not getattr(self.api, "gift_sender", None):
            self.api.gift_sender = PersonalGiftSender(settings)
        return GiftService(self.store, self.api, settings, self.api.gift_sender)

    def configured_gifts(self, item):
        listing = self.listing(item["id"])
        if not listing or listing["state"] == "LEGACY":
            return None
        if (
            listing["handler"] != "telegram_gifts"
            or listing["state"] != "VERIFIED"
            or not listing["verified_at"]
            or listing["revision"] != listing["config_revision"]
            or marker(item.get("description")) != listing["code"]
            or critical(item) != json.loads(listing["baseline"])
            or digest(item["description"]) != listing["description_sha256"]
        ):
            raise Conflict("Постоянный код или настройки подарочного товара изменились")
        return json.loads(listing["config"])["code"]

    def migrate_legacy(self):
        """Local projection only: leave old bindings/intents/permissions unchanged."""
        account = self.store.setting("playerok:account_id")
        if not account:
            return 0
        imported = 0
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT g.* FROM gift_listings g WHERE NOT EXISTS "
                "(SELECT 1 FROM logical_listings l WHERE l.item_id=g.item_id)"
            ).fetchall()
            for row in rows:
                options = self.store.setting("gifts:listing_options:" + row["item_id"], {})
                options = {**options, "refund": options.get("refund", True)}
                config = {
                    "code": row["gift_code"],
                    "bundle": json.loads(row["bundle"]),
                    "auto_fulfill": bool(row["auto_fulfill"]),
                    "options": options,
                    "after": options.get("after", ""),
                }
                ident = uuid.uuid4().hex
                for _ in range(32):
                    code = "#" + str(1_000_000 + secrets.randbelow(9_000_000))
                    try:
                        db.execute(
                            "INSERT INTO logical_products(id,code,owner_id,account_id,handler,config,state,stock_item_id,baseline,original_description,created_at,updated_at) "
                            "VALUES(?,?,?,?,'telegram_gifts',?,'LEGACY',?,'{}','',?,?)",
                            (
                                ident,
                                code,
                                row["owner_id"],
                                account,
                                encode(config),
                                row["item_id"],
                                now(),
                                now(),
                            ),
                        )
                        break
                    except sqlite3.IntegrityError:
                        if not db.execute("SELECT 1 FROM logical_products WHERE code=?", (code,)).fetchone():
                            raise
                else:
                    raise Conflict("Не удалось выделить код для переноса привязки")
                db.execute(
                    "INSERT INTO logical_listings(item_id,product_id,config_revision,description_sha256,server_status) VALUES(?,?,1,?,'UNVERIFIED')",
                    (row["item_id"], ident, row["description_sha256"]),
                )
                imported += 1
        return imported

    async def prepare_creation(self, owner, value):
        """Read-only preparation for a new wizard-created private draft."""
        account = self.authority(owner, writes=True)
        handler = {"gifts": "telegram_gifts", "text": "fixed_text", "stock": "unique_stock"}.get(
            value.get("delivery")
        )
        if not handler:
            raise Conflict("Выберите автоматический способ выдачи")
        if marker(value["description"]):
            raise Conflict("Новый товар не может присвоить существующий постоянный код")
        gifts = self.gift_service() if handler == "telegram_gifts" else None
        config = configuration(
            handler,
            {
                "code": value.get("gift_code", ""),
                **({"text": value.get("delivery_data", "")} if handler == "fixed_text" else {}),
                "after": "",
                "greeting": "",
            },
            max_count=gifts.settings.max_gifts if gifts else 5,
        )
        if handler == "telegram_gifts":
            if (
                value.get("category_id") != gifts.settings.category_id
                or value.get("obtaining_type_id") != gifts.settings.obtaining_type_id
            ):
                raise Conflict("Категория или способ передачи не соответствует Telegram-подарку")
            await gifts.refresh()
            config["bundle"] = gifts.catalog_bundle(config["code"])
            if config["bundle"] != value.get("gift_bundle"):
                raise Conflict("Цена или доступность подарков изменилась. Выберите их заново")
        return {"account": account, "handler": handler, "config": config}

    def commit_creation(self, db, owner, draft_id, value, prepared):
        self.authority(owner, prepared["account"], writes=True)
        code = None
        for _ in range(32):
            candidate = "#" + str(1_000_000 + secrets.randbelow(9_000_000))
            if not db.execute("SELECT 1 FROM logical_products WHERE code=?", (candidate,)).fetchone():
                code = candidate
                break
        if code is None:
            raise Conflict("Не удалось выделить постоянный код")
        ident = uuid.uuid4().hex
        original = value["description"]
        value = {
            **value,
            "managed_product_id": ident,
            "permanent_code": code,
            "description": listing_text(marked_description(original, code), "description"),
        }
        db.execute(
            "INSERT INTO logical_products(id,code,owner_id,account_id,handler,config,state,stock_item_id,baseline,original_description,created_at,updated_at) VALUES(?,?,?,?,?,?,'PENDING',?,'{}',?,?,?)",
            (
                ident,
                code,
                owner,
                prepared["account"],
                prepared["handler"],
                encode(prepared["config"]),
                draft_id,
                original,
                now(),
                now(),
            ),
        )
        db.execute("UPDATE drafts SET payload=? WHERE id=?", (encode(value), draft_id))
        return value

    def creation_proof(self, owner, value, observed):
        from app.services.item_receipts import edit_receipt_matches

        product = self.product(value["managed_product_id"], owner)
        self.authority(owner, product["account_id"])
        if (
            marker(observed.get("description")) != product["code"]
            or not edit_receipt_matches(
                observed.get("id"), {key: value[key] for key in ("name", "description", "price")}, observed
            )
            or (observed.get("category") or {}).get("id") != value["category_id"]
            or (observed.get("obtaining_type") or {}).get("id") != value["obtaining_type_id"]
        ):
            raise Conflict("Созданный товар не совпадает с подтверждённым черновиком")
        baseline = critical(observed)
        if baseline["account"] != product["account_id"] or observed.get("status") != "DRAFT":
            raise Conflict("Созданный товар не подтверждён как черновик своего магазина")
        return {
            "product": product["id"],
            "remote": observed["id"],
            "baseline": baseline,
            "description_hash": digest(observed["description"]),
        }

    def confirm_creation(self, db, owner, draft_id, value, observed):
        self.commit_creation_proof(db, owner, draft_id, value, self.creation_proof(owner, value, observed))

    def commit_creation_proof(self, db, owner, draft_id, value, proof):
        product = self.product(value["managed_product_id"], owner)
        self.authority(owner, product["account_id"])
        if (
            proof["product"] != product["id"]
            or proof["baseline"]["account"] != product["account_id"]
            or proof["description_hash"] != digest(value["description"])
        ):
            raise Conflict("Подтверждение создания не соответствует товару")
        existing = self.listing(proof["remote"])
        if existing:
            if existing["id"] != product["id"] or existing["description_sha256"] != proof["description_hash"]:
                raise Conflict("Созданный товар уже связан с другой настройкой")
        elif product["state"] != "PENDING" or product["stock_item_id"] != draft_id or product["enabled"]:
            raise Conflict("Подготовленная привязка создания изменилась")
        else:
            db.execute(
                "UPDATE logical_products SET stock_item_id=?,baseline=?,state='VERIFIED',updated_at=? WHERE id=?",
                (proof["remote"], encode(proof["baseline"]), now(), product["id"]),
            )
            db.execute(
                "INSERT INTO logical_listings(item_id,product_id,config_revision,description_sha256,server_status,verified_at) VALUES(?,?,1,?,'DRAFT',?)",
                (proof["remote"], product["id"], proof["description_hash"], now()),
            )
        binding = db.execute("SELECT remote_id FROM bindings WHERE draft_id=?", (draft_id,)).fetchone()
        if binding and binding[0] != proof["remote"]:
            raise Conflict("Черновик связан с другим серверным товаром")
        db.execute(
            "INSERT OR IGNORE INTO bindings(draft_id,remote_id) VALUES(?,?)", (draft_id, proof["remote"])
        )
        db.execute("UPDATE drafts SET state='REMOTE_DRAFT' WHERE id=?", (draft_id,))
        if value.get("cost") is not None:
            self.store.set_setting(f"finance:{owner}:cost:{proof['remote']}", value["cost"])

    async def prepare_legacy_description(self, owner, item_id):
        existing = self.listing(item_id)
        if not existing or existing["state"] != "LEGACY" or existing["handler"] != "telegram_gifts":
            raise Conflict("Перенос недоступен. Обновите карточку привязки")
        return await self.prepare(owner, item_id, existing["handler"], {}, migrate_legacy=True)

    async def prepare(self, owner, item_id, handler, config, *, migrate_legacy=False):
        account = self.authority(owner)
        item = await self.api.call("get_item", id=item_id)
        baseline = critical(item)
        if item.get("id") != item_id or baseline["account"] != account:
            raise Conflict("Товар принадлежит другому аккаунту")
        if item.get("is_editable") is False:
            raise Conflict("Playerok запрещает редактирование этого товара")
        if item.get("deleted_at") or item.get("status") not in {"DRAFT", "APPROVED"}:
            raise ValueError("Привязка доступна для опубликованного товара или серверного черновика")
        existing = self.listing(item_id)
        if existing and existing["enabled"]:
            raise Conflict("Сначала остановите выдачу товара")
        if existing:
            self.assert_editable_intents(existing["id"])
        if migrate_legacy:
            if not existing or existing["state"] != "LEGACY" or handler != "telegram_gifts":
                raise Conflict("Старая привязка изменилась. Обновите карточку")
            # Description-only migration preserves the already approved snapshot.
            # No MTProto, price reconfirmation, spending permission or automatic arm.
            config = json.loads(existing["config"])
            if (
                gift_code_from_description(
                    item.get("description", ""), max_count=max(5, len(config.get("bundle", [])))
                )
                != config["code"]
            ):
                raise Conflict("Метка подарка не совпадает со старой привязкой")
        gifts = self.gift_service() if handler == "telegram_gifts" and not migrate_legacy else None
        if not migrate_legacy:
            config = configuration(handler, config, max_count=gifts.settings.max_gifts if gifts else 5)
        if handler == "telegram_gifts" and not migrate_legacy:
            inspected = await gifts.inspect_item(owner, item_id, preview_code=config["code"])
            if critical(inspected["item"]) != baseline or inspected["item"].get("description") != item.get(
                "description"
            ):
                raise Conflict("Товар изменился во время проверки подарков")
            balance = await gifts.refresh()
            config["bundle"] = gifts.catalog_bundle(config["code"])
            total = sum(g["stars"] for g in config["bundle"])
            global_order, global_day = gifts.limits()
            options = config.get("options") or {}
            order_limit, day_limit = options.get("order", global_order), options.get("day", global_day)
            if (
                type(order_limit) is not int
                or type(day_limit) is not int
                or not 0 < order_limit <= global_order
                or not order_limit <= day_limit <= global_day
            ):
                raise Conflict(
                    "Лимиты товара должны быть положительными и не превышать общие лимиты магазина"
                )
            if total > order_limit or total > day_limit:
                raise Conflict("Стоимость пакета превышает выбранный лимит Stars")
            refund = options.get("refund", False)
            if type(refund) is not bool:
                raise Conflict("Возврат для товара требует явного выбора: включён или выключен")
            config["options"] = {"order": order_limit, "day": day_limit, "refund": refund}
            if total > balance:
                raise Conflict("Недостаточно Stars для настроенного пакета подарков")
        description = item.get("description")
        existing_marker = marker(description)
        if existing_marker and (not existing or existing["code"] != existing_marker):
            raise Conflict("Код в описании не принадлежит этой привязке")
        cleaned = description
        if handler == "telegram_gifts":
            lines = description.splitlines()
            labels = [i for i, line in enumerate(lines) if line.strip().lower().startswith("gift:")]
            if labels:
                if len(labels) != 1 or labels[0] != max(i for i, line in enumerate(lines) if line.strip()):
                    raise ValueError("Старая метка gift неоднозначна; сначала исправьте описание")
                cleaned = "\n".join(lines[: labels[0]]).rstrip()
        code = existing["code"] if existing else None
        if code is None:
            for _ in range(32):
                candidate = "#" + str(1_000_000 + secrets.randbelow(9_000_000))
                if not self.store.rows("SELECT 1 FROM logical_products WHERE code=?", (candidate,)):
                    code = candidate
                    break
            if code is None:
                raise Conflict("Не удалось выделить код. Повторите подготовку")
        return {
            "request_id": uuid.uuid4().hex,
            "owner": owner,
            "account": account,
            "item_id": item_id,
            "handler": handler,
            "config": config,
            "baseline": baseline,
            "original": description,
            "cleaned": cleaned,
            "status": item["status"],
            "product_id": existing["id"] if existing else None,
            "revision": existing["revision"] if existing else 0,
            "control_revision": existing["control_revision"] if existing else 0,
            "code": code,
        }

    def commit(self, owner, prepared):
        self.authority(owner, prepared["account"], writes=True)
        if prepared["owner"] != owner:
            raise Conflict("Подтверждение принадлежит другому владельцу")
        with self.store.transaction() as db:
            key = "logical-bind:" + prepared["request_id"]
            old = db.execute("SELECT id,payload FROM outbox WHERE operation_key=?", (key,)).fetchone()
            if old:
                return {
                    "product_id": json.loads(old["payload"])["logical_binding"]["product_id"],
                    "operation_id": old["id"],
                }
            current_link = self.listing(prepared["item_id"])
            if (
                prepared["product_id"] is None
                and current_link is not None
                or prepared["product_id"] is not None
                and (
                    not current_link
                    or current_link["id"] != prepared["product_id"]
                    or current_link["revision"] != prepared["revision"]
                    or current_link["control_revision"] != prepared["control_revision"]
                )
            ):
                raise Conflict("Привязка изменилась после превью. Откройте актуальную настройку")
            ident = prepared["product_id"] or uuid.uuid4().hex
            if prepared["product_id"]:
                product = self.product(ident, owner)
                self.assert_editable_intents(ident)
                if product["revision"] != prepared["revision"] or product["enabled"]:
                    raise Conflict("Настройки изменились. Подготовьте привязку заново")
                code = product["code"]
                revision = product["revision"] + 1
            else:
                revision = 1
                code = prepared["code"]
                try:
                    db.execute(
                        "INSERT INTO logical_products(id,code,owner_id,account_id,handler,config,state,stock_item_id,baseline,original_description,created_at,updated_at) VALUES(?,?,?,?,?,?,'PENDING',?,?,?,?,?)",
                        (
                            ident,
                            code,
                            owner,
                            prepared["account"],
                            prepared["handler"],
                            encode(prepared["config"]),
                            prepared["item_id"],
                            encode(prepared["baseline"]),
                            prepared["original"],
                            now(),
                            now(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    if not db.execute("SELECT 1 FROM logical_products WHERE code=?", (code,)).fetchone():
                        raise
                    raise Conflict(
                        "Код уже занят. Обновите превью: новый код требует подтверждения"
                    ) from None
            description = listing_text(marked_description(prepared["cleaned"], code), "description")
            db.execute(
                "UPDATE logical_products SET handler=?,config=?,revision=?,enabled=0,state='PENDING',baseline=?,original_description=?,updated_at=? WHERE id=?",
                (
                    prepared["handler"],
                    encode(prepared["config"]),
                    revision,
                    encode(prepared["baseline"]),
                    prepared["original"],
                    now(),
                    ident,
                ),
            )
            operation = self.store.enqueue(
                key,
                "edit",
                prepared["item_id"],
                {
                    "description": description,
                    "logical_binding": {
                        "product_id": ident,
                        "revision": revision,
                        "account": prepared["account"],
                        "original_hash": digest(prepared["original"]),
                        "baseline": prepared["baseline"],
                    },
                },
                db,
                actor=owner,
            )
            db.execute(
                "INSERT INTO logical_listings(item_id,product_id,config_revision,description_sha256,server_status,operation_id) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(item_id) DO UPDATE SET config_revision=excluded.config_revision,description_sha256=excluded.description_sha256,server_status=excluded.server_status,verified_at=NULL,operation_id=excluded.operation_id",
                (prepared["item_id"], ident, revision, digest(description), prepared["status"], operation),
            )
            return {"product_id": ident, "operation_id": operation}

    def assert_editable_intents(self, product_id):
        if self.store.rows(
            "SELECT 1 FROM logical_listings l LEFT JOIN gift_listings g ON g.item_id=l.item_id "
            "LEFT JOIN delivery_policies p ON p.item_id=l.item_id WHERE l.product_id=? "
            "AND (g.enabled=1 OR json_extract(p.payload,'$.enabled')=1) LIMIT 1",
            (product_id,),
        ):
            raise Conflict(
                "Сначала остановите действующую выдачу товара, затем меняйте способ или переносите привязку"
            )
        if self.store.rows(
            "SELECT 1 FROM gift_jobs j JOIN logical_listings l ON l.item_id=j.item_id "
            "WHERE l.product_id=? AND (j.state IN ('PENDING','CHECKING','SPENDING','UNCERTAIN') "
            "OR j.refund_state IN ('QUEUED','SPENDING','UNCERTAIN') "
            "OR j.state='BLOCKED' AND EXISTS(SELECT 1 FROM gift_steps s WHERE s.order_id=j.order_id AND s.spent_at IS NOT NULL)) LIMIT 1",
            (product_id,),
        ) or self.store.rows(
            "SELECT 1 FROM delivery_chains c JOIN logical_listings l ON l.item_id=c.item_id WHERE l.product_id=? AND c.status='ACTIVE' LIMIT 1",
            (product_id,),
        ):
            raise Conflict(
                "Сначала завершите или безопасно разрешите текущую выдачу товара. Её конфигурация сохранена"
            )

    def binding_guard(self, action, current=None, *, writes=True):
        payload = action["payload"]["logical_binding"]
        product = self.product(payload["product_id"])
        self.authority(product["owner_id"], payload["account"], writes=writes)
        listing = self.listing(action["target"])
        if (
            not listing
            or listing["id"] != product["id"]
            or listing["operation_id"] != action["id"]
            or product["revision"] != payload["revision"]
            or product["state"] != "PENDING"
        ):
            raise Conflict("Подтверждение привязки устарело")
        if current is not None and (
            current.get("id") != action["target"]
            or critical(current) != payload["baseline"]
            or digest(current.get("description", "")) != payload["original_hash"]
        ):
            raise Conflict("Товар изменился после превью. Обновите данные и подтвердите заново")

    def confirm_binding(self, action, observed, *, writes=True):
        self.binding_guard(action, writes=writes)
        expected = action["payload"]["description"]
        product = self.product(action["payload"]["logical_binding"]["product_id"])
        if (
            observed.get("id") != action["target"]
            or critical(observed) != action["payload"]["logical_binding"]["baseline"]
            or observed.get("description") != expected
            or marker(expected) != product["code"]
        ):
            raise Conflict("Playerok не подтвердил точное описание и владельца привязки")
        return {
            "product_id": product["id"],
            "revision": product["revision"],
            "item_id": action["target"],
            "status": observed["status"],
            "description_sha256": digest(expected),
        }

    async def reconcile_binding(self, owner, product_id):
        product = self.product(product_id, owner)
        self.authority(owner, product["account_id"])
        rows = self.store.rows(
            "SELECT o.* FROM outbox o JOIN logical_listings l ON l.operation_id=o.id "
            "WHERE l.product_id=? AND l.config_revision=? AND o.status='UNCERTAIN'",
            (product_id, product["revision"]),
        )
        if len(rows) != 1:
            raise Conflict("Нет единственной неизвестной записи описания для сверки")
        action = {**rows[0], "payload": json.loads(rows[0]["payload"])}
        observed = await self.api.call("get_item", id=action["target"])
        receipt = self.confirm_binding(action, observed, writes=False)
        result = {"logical_binding": receipt, "resolution_source": "INDEPENDENT_PLAYEROK_READ"}
        with self.store.transaction() as db:
            self.binding_guard(action, writes=False)
            changed = db.execute(
                "UPDATE outbox SET status='SUCCEEDED',result=?,error_code=NULL WHERE id=? AND status='UNCERTAIN' AND payload=?",
                (encode(result), action["id"], rows[0]["payload"]),
            ).rowcount
            if changed != 1:
                raise Conflict("Исход операции уже изменился. Обновите карточку")
            finish_binding(db, action, "SUCCEEDED", result)
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'RECONCILE_LOGICAL_BINDING',?,'CONFIRMED_READ_ONLY',?)",
                (owner, action["id"], now()),
            )
        return receipt

    def verify_listing(self, product, item):
        link = self.listing(item.get("id"))
        if (
            product["state"] != "VERIFIED"
            or not link
            or link["id"] != product["id"]
            or not link["verified_at"]
            or link["config_revision"] != product["revision"]
            or marker(item.get("description")) != product["code"]
            or critical(item) != json.loads(product["baseline"])
            or digest(item["description"]) != link["description_sha256"]
        ):
            raise Conflict("Код, описание или критичные параметры товара не подтверждены")

    async def arm(self, owner, product_id, revision, *, inherit=False, item_id=None):
        product = self.product(product_id, owner)
        self.authority(owner, product["account_id"], writes=True)
        if not getattr(self.api.account, "automation_enabled", False):
            raise Conflict("Автоматизация выключена в настройках подключения")
        if product["revision"] != revision or product["enabled"] or product["handler"] == "manual":
            raise Conflict("Настройки изменились, выдача уже включена или выбран ручной режим")
        item = await self.api.call("get_item", id=item_id or product["stock_item_id"])
        self.verify_listing(product, item)
        if item.get("status") != "APPROVED":
            raise Conflict("Включение доступно для опубликованного товара")
        config = json.loads(product["config"])
        arm_context = {
            "product_id": product_id,
            "revision": revision,
            "control_revision": product["control_revision"],
            "inherit": bool(inherit),
        }
        if product["handler"] == "telegram_gifts":
            gifts = self.gift_service()
            await gifts.bind(owner, item["id"], config["code"], expected_bundle=config["bundle"])
            listing = gifts.listing(item["id"])
            await gifts.arm(
                owner,
                item["id"],
                listing["revision"],
                auto_fulfill=config.get("auto_fulfill", False),
                logical_arm=arm_context,
            )
            cutoff = gifts.listing(item["id"])["armed_at"]
        else:
            policy = DeliveryPolicy(
                enabled=True,
                auto_enroll=True,
                mode="stock" if product["handler"] == "unique_stock" else "text",
                text=config.get("text", ""),
                greeting=config.get("greeting", ""),
                after=config.get("after", ""),
                mark_sent=config.get("mark_sent", False),
            )
            automation = Automation(self.store, self.api)
            old_revision, _ = automation.get(item["id"])
            await automation.save(
                owner,
                item["id"],
                old_revision,
                asdict(policy),
                future_confirmed=True,
                logical_arm=arm_context,
            )
            cutoff = self.store.rows(
                "SELECT updated_at FROM delivery_policies WHERE item_id=?", (item["id"],)
            )[0]["updated_at"]
        return cutoff

    def commit_arm(self, db, owner, item_id, context, cutoff):
        """Same transaction as the existing execution policy: STOP always fences late arm."""
        product = self.product(context["product_id"], owner)
        link = self.listing(item_id)
        self.authority(owner, product["account_id"], writes=True)
        if (
            not getattr(self.api.account, "automation_enabled", False)
            or product["revision"] != context["revision"]
            or product["control_revision"] != context["control_revision"]
            or product["enabled"]
            or product["state"] != "VERIFIED"
            or not link
            or link["id"] != product["id"]
            or not link["verified_at"]
            or link["config_revision"] != product["revision"]
        ):
            raise Conflict(
                "Выдача остановлена или настройки изменились во время проверки. Подтвердите включение заново"
            )
        if (
            product["handler"] == "unique_stock"
            and not db.execute(
                "SELECT 1 FROM inventory WHERE item_id=? AND status='AVAILABLE' LIMIT 1",
                (product["stock_item_id"],),
            ).fetchone()
        ):
            raise Conflict("На складе нет доступных единиц товара")
        if product["handler"] == "telegram_gifts":
            config = json.loads(product["config"])
            self.store.set_setting(
                "gifts:listing_options:" + item_id,
                {**config.get("options", {}), "after": config.get("after", "")},
            )
        changed = db.execute(
            "UPDATE logical_products SET enabled=1,inherit_enabled=?,armed_at=?,updated_at=? WHERE id=? AND revision=? AND control_revision=? AND enabled=0",
            (
                int(context["inherit"]),
                cutoff,
                now(),
                product["id"],
                context["revision"],
                context["control_revision"],
            ),
        ).rowcount
        if changed != 1:
            raise Conflict("Право включения изменилось; повторите проверку")

    async def adopt(
        self,
        owner,
        product_id,
        item,
        complete_snapshot,
        *,
        paid_order=None,
        source_order_id=None,
        snapshot_item_ids=(),
    ):
        """Called only with a completed account scan; never discover or spend here."""
        product = self.product(product_id, owner)
        self.authority(owner, product["account_id"], writes=True)
        if not product["enabled"] or not product["inherit_enabled"] or product["state"] != "VERIFIED":
            raise Conflict("Автоматическое наследование для этого товара не разрешено")
        if (
            critical(item) != json.loads(product["baseline"])
            or marker(item.get("description")) != product["code"]
            or item.get("status") not in ({"APPROVED", "SOLD"} if paid_order else {"APPROVED"})
            or item.get("deleted_at")
        ):
            raise Conflict("Перевыставленный товар не совпадает с подтверждёнными параметрами")
        if source_order_id and (
            not paid_order
            or paid_order.get("id") != source_order_id
            or product["handler"] != "telegram_gifts"
        ):
            raise Conflict("Снимок сделки не разрешает этот способ выдачи")
        active = []
        for observed in complete_snapshot:
            if (observed.get("user") or {}).get("id") != product["account_id"]:
                raise Conflict("Каталог содержит товар другого аккаунта")
            if (
                observed.get("id") not in snapshot_item_ids
                and observed.get("status") == "APPROVED"
                and not observed.get("deleted_at")
                and marker(observed.get("description")) == product["code"]
            ):
                active.append(observed["id"])
        if paid_order is not None:
            from app.services.order_management import deliverable

            deliverable(paid_order, paid_order.get("id"), product["account_id"])
            if (paid_order.get("item") or {}).get("id") != item["id"] or not future_order(
                paid_order.get("created_at"), product["armed_at"]
            ):
                raise Conflict("Оплаченный заказ не подтверждает новое объявление этой настройки")
        if (not source_order_id and item.get("status") == "APPROVED" and set(active) != {item["id"]}) or len(
            set(active)
        ) > 1:
            raise Conflict("Обнаружены активные копии кода или полнота каталога не подтверждена")
        existing = self.listing(item["id"])
        if existing:
            self.verify_listing(product, item)
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE logical_listings SET server_status=?,verified_at=? WHERE item_id=? AND config_revision=?",
                    (item["status"], now(), item["id"], product["revision"]),
                )
                db.execute(
                    "UPDATE logical_products SET error_code=NULL WHERE id=? AND revision=?",
                    (product_id, product["revision"]),
                )
            return item["id"]
        links = self.store.rows(
            "SELECT * FROM logical_listings WHERE product_id=? AND config_revision=? AND verified_at IS NOT NULL",
            (product_id, product["revision"]),
        )
        if not links or any(link["description_sha256"] != digest(item["description"]) for link in links):
            raise Conflict("Описание нового объявления отличается от подтверждённого")
        config = json.loads(product["config"])
        if product["handler"] == "telegram_gifts":
            gifts = self.gift_service()
            await gifts.refresh()
            if gifts.catalog_bundle(config["code"]) != config["bundle"]:
                raise Conflict("Цена или доступность подарков изменилась; подтвердите настройки")
        with self.store.transaction() as db:
            current = self.product(product_id)
            if (
                not current["enabled"]
                or current["revision"] != product["revision"]
                or not current["inherit_enabled"]
            ):
                raise Conflict("Наследование остановлено во время проверки")
            self.authority(owner, product["account_id"], writes=True)
            if source_order_id:
                from app.services.order_management import deliverable

                row = db.execute("SELECT payload FROM orders WHERE id=?", (source_order_id,)).fetchone()
                if not row:
                    raise Conflict("Сделка не найдена")
                local_order = json.loads(row[0])
                deliverable(local_order, source_order_id, product["account_id"])
                if (local_order.get("item") or {}).get("id") != item["id"]:
                    raise Conflict("Снимок относится к другой сделке")
            source = product["stock_item_id"]
            source_cost = self.store.setting(f"finance:{owner}:cost:{source}")
            target_cost = self.store.setting(f"finance:{owner}:cost:{item['id']}")
            if source_cost is not None and target_cost is None:
                self.store.set_setting(f"finance:{owner}:cost:{item['id']}", source_cost)
            if product["handler"] == "telegram_gifts":
                sources = db.execute(
                    "SELECT l.item_id FROM logical_listings l JOIN gift_listings g ON g.item_id=l.item_id "
                    "WHERE l.product_id=? AND l.config_revision=? AND l.verified_at IS NOT NULL AND g.enabled=1 "
                    "ORDER BY l.verified_at DESC LIMIT 1",
                    (product_id, product["revision"]),
                ).fetchall()
                if sources:
                    source = sources[0]["item_id"]
                old = self.gift_service().listing(source)
                if not old or not old["enabled"] or self.store.setting("gifts:stopped", True):
                    raise Conflict("Подарочная выдача исходного товара остановлена")
                columns = [key for key in old if key != "item_id"]
                db.execute(
                    "INSERT INTO gift_listings(item_id,"
                    + ",".join(columns)
                    + ") VALUES("
                    + ",".join("?" for _ in range(len(columns) + 1))
                    + ")",
                    (item["id"], *(old[key] for key in columns)),
                )
                self.store.set_setting(
                    "gifts:listing_options:" + item["id"],
                    self.store.setting("gifts:listing_options:" + source, {}),
                )
            elif product["handler"] in {"fixed_text", "unique_stock"}:
                sources = db.execute(
                    "SELECT l.item_id FROM logical_listings l JOIN delivery_policies p ON p.item_id=l.item_id "
                    "WHERE l.product_id=? AND l.config_revision=? AND l.verified_at IS NOT NULL AND json_extract(p.payload,'$.enabled')=1 "
                    "ORDER BY l.verified_at DESC LIMIT 1",
                    (product_id, product["revision"]),
                ).fetchall()
                if sources:
                    source = sources[0]["item_id"]
                old = db.execute("SELECT * FROM delivery_policies WHERE item_id=?", (source,)).fetchone()
                if (
                    not old
                    or not json.loads(old["payload"]).get("enabled")
                    or self.store.setting(f"delivery:{source}:enabled") is False
                ):
                    raise Conflict("Выдача исходного товара остановлена")
                db.execute(
                    "INSERT INTO delivery_policies VALUES(?,?,?,?,?)",
                    (item["id"], old["revision"], owner, old["payload"], product["armed_at"]),
                )
            db.execute(
                "INSERT INTO logical_listings(item_id,product_id,config_revision,description_sha256,server_status,verified_at,source_order_id) VALUES(?,?,?,?,?,?,?)",
                (
                    item["id"],
                    product_id,
                    product["revision"],
                    digest(item["description"]),
                    item["status"],
                    now(),
                    source_order_id,
                ),
            )
            db.execute(
                "UPDATE logical_products SET error_code=NULL WHERE id=? AND revision=?",
                (product_id, product["revision"]),
            )
        return item["id"]

    def stop(self, owner, product_id):
        product = self.product(product_id, owner)
        with self.store.transaction() as db:
            for link in db.execute(
                "SELECT item_id FROM logical_listings WHERE product_id=?", (product_id,)
            ).fetchall():
                db.execute(
                    "UPDATE gift_listings SET enabled=0,revision=revision+1 WHERE item_id=? AND enabled=1",
                    (link["item_id"],),
                )
                row = db.execute(
                    "SELECT payload FROM delivery_policies WHERE item_id=?", (link["item_id"],)
                ).fetchone()
                if row:
                    policy = json.loads(row["payload"])
                    if policy.get("enabled"):
                        policy["enabled"] = False
                        db.execute(
                            "UPDATE delivery_policies SET payload=?,revision=revision+1 WHERE item_id=?",
                            (encode(policy), link["item_id"]),
                        )
            db.execute(
                "UPDATE logical_products SET enabled=0,control_revision=control_revision+1,updated_at=? WHERE id=? AND owner_id=?",
                (now(), product["id"], owner),
            )

    def detach(self, owner, product_id, revision):
        with self.store.transaction() as db:
            product = self.product(product_id, owner)
            if product["revision"] != revision:
                raise Conflict("Настройки изменились. Подтвердите отвязку заново")
            self.stop(owner, product_id)
            db.execute(
                "UPDATE logical_products SET state='DETACHED',inherit_enabled=0,revision=revision+1 WHERE id=?",
                (product_id,),
            )

    def require_delivery(self, item_id, handler=None, order_id=None):
        binding = self.listing(item_id)
        if not binding or binding["state"] == "LEGACY":
            return None
        self.authority(binding["owner_id"], binding["account_id"])
        if (
            binding["state"] != "VERIFIED"
            or not binding["enabled"]
            or not binding["verified_at"]
            or binding["revision"] != binding["config_revision"]
            or handler
            and binding["handler"] != handler
            or not order_id
            and binding["error_code"] is not None
        ):
            raise Conflict("Постоянная привязка не готова или выдача остановлена")
        if order_id:
            if binding["source_order_id"] and binding["source_order_id"] != order_id:
                raise Conflict("Снимок товара принадлежит другой сделке")
            scopes = self.store.rows("SELECT * FROM logical_order_scopes WHERE order_id=?", (order_id,))
            if not scopes or any(
                scope["product_id"] != binding["id"]
                or scope["config_revision"] != binding["revision"]
                or scope["item_id"] != item_id
                or scope["config"] != binding["config"]
                for scope in scopes
            ):
                raise Conflict("Конфигурация заказа не совпадает с подтверждённой привязкой")
        return binding

    async def fresh_delivery_item(self, item_id):
        product = self.listing(item_id)
        if not product or product["state"] == "LEGACY":
            return
        item = await self.api.call("get_item", id=item_id)
        if (
            item.get("id") != item_id
            or item.get("deleted_at")
            or item.get("status") not in {"APPROVED", "SOLD"}
        ):
            raise Conflict("Состояние объявления не подтверждает выдачу оплаченного товара")
        self.verify_listing(product, item)

    def pin_order(self, db, order_id, item_id, handler):
        product = self.require_delivery(item_id, handler)
        if product is None:
            return
        if product["source_order_id"] and product["source_order_id"] != order_id:
            raise Conflict("Снимок товара принадлежит другой сделке")
        row = db.execute("SELECT payload,status,item_id FROM orders WHERE id=?", (order_id,)).fetchone()
        if (
            not row
            or row["status"] != "PAID"
            or row["item_id"] != item_id
            or not future_order(json.loads(row["payload"]).get("created_at"), product["armed_at"])
        ):
            raise Conflict("Заказ не подтверждён как новый оплаченный заказ этой конфигурации")
        db.execute(
            "INSERT OR IGNORE INTO logical_order_scopes VALUES(?,?,?,?,?,?,?)",
            (
                order_id,
                product["id"],
                product["revision"],
                item_id,
                product["stock_item_id"],
                product["config"],
                now(),
            ),
        )
        self.require_delivery(item_id, handler, order_id)


def stock_scope(store, item_id, order_id=None):
    if order_id:
        rows = store.rows(
            "SELECT stock_item_id,item_id FROM logical_order_scopes WHERE order_id=?", (order_id,)
        )
        if rows:
            if rows[0]["item_id"] != item_id:
                raise Conflict("Склад закреплён за другим товаром заказа")
            return rows[0]["stock_item_id"]
    product = LogicalProducts(store).listing(item_id)
    if (
        product
        and product["state"] == "VERIFIED"
        and product["handler"] == "unique_stock"
        and product["verified_at"]
    ):
        return product["stock_item_id"]
    return item_id


def refund_allowed(store, item_id, order_id):
    options = store.setting("gifts:listing_options:" + item_id, {})
    if not options.get("refund", True):
        return False
    scopes = store.rows(
        "SELECT config FROM logical_order_scopes WHERE order_id=? AND item_id=?", (order_id, item_id)
    )
    return not scopes or json.loads(scopes[0]["config"]).get("options", {}).get("refund") is True


def finish_binding(db, action, status, result):
    if status != "SUCCEEDED" or not result or not result.get("logical_binding"):
        return
    receipt = result["logical_binding"]
    db.execute(
        "UPDATE logical_listings SET verified_at=?,server_status=? WHERE item_id=? AND operation_id=? AND config_revision=?",
        (now(), receipt["status"], receipt["item_id"], action["id"], receipt["revision"]),
    )
    db.execute(
        "UPDATE logical_products SET state='VERIFIED',updated_at=? WHERE id=? AND revision=? AND state='PENDING'",
        (now(), receipt["product_id"], receipt["revision"]),
    )
