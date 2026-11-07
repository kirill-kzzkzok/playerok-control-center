"""Identity and verified item projections; execution stays in Gifts/DeliveryChains."""

import asyncio
import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict

from app.database.store import Conflict, encode, now
from app.gifts.core import gift_code_from_description
from app.playerok.adapter import APIError
from app.security.logging import event
from app.services.automation import Automation, DeliveryPolicy, future_order, message_text
from app.services.product_codes import choose_code, reserve_code, title_code, titled
from app.services.products import field_baseline, listing_text

PROBLEMS = {
    "CATALOG_MARKER_INVALID": "В названии обнаружен некорректный код. Проверьте связанные объявления.",
    "DUPLICATE_ACTIVE_MARKER": "Найдено несколько активных объявлений с одним кодом. Разрешите конфликт.",
    "RELIST_CRITICAL_FIELDS_CHANGED": "Критические параметры нового объявления отличаются от подтверждённого товара.",
    "RELIST_ITEM_CHANGED": "Перевыставленный товар изменился во время фоновой проверки. Бот повторит сверку автоматически.",
    "RELIST_PERMISSION_MISSING": "Автоматическое наследование этого товара не разрешено.",
    "RELIST_BINDING_CONFLICT": "Серверные данные объявления противоречат сохранённой привязке.",
    "RELIST_CANDIDATE_INVALID": "Кандидат не подтверждён как объявление этого товара и аккаунта.",
    "RELIST_SOURCE_UNVERIFIED": "Нет подтверждённого исходного объявления для безопасного наследования.",
    "RELIST_GIFT_CONFIGURATION_CHANGED": "Изменились цена или доступность выбранного подарка; нужна настройка состава выдачи.",
    "RELIST_CONTROL_CHANGED": "Наследование остановлено во время проверки: настройки или остановка изменились; старое наблюдение отклонено.",
    "RELIST_CONFIRMATION_REQUIRED": "Новое объявление не прошло проверку. Проверьте его параметры и настройки.",
}


class RelistConflict(Conflict):
    """Safe classified evidence; never carries raw API responses or credentials."""

    def __init__(self, code):
        self.code = code
        super().__init__(PROBLEMS[code])


HANDLERS = frozenset({"telegram_gifts", "unique_stock", "fixed_text", "manual"})


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def marker(name):
    return title_code(name)


def marked_title(name, code):
    return titled(name, code)


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
        "name": marker(item["name"]) or item["name"],
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


def without_messages(config, *, completion=False):
    """Future messages/completion may vary; delivery content and STOP cannot."""
    value = json.loads(config) if isinstance(config, str) else config
    ignored = {"greeting", "after"}
    if completion:
        ignored |= {"auto_fulfill", "mark_sent", "automatic_completion"}
    return {key: selected for key, selected in value.items() if key not in ignored}


class LogicalProducts:
    def __init__(self, store, api=None):
        self.store, self.api = store, api

    def update_after(self, owner, item_id, expected, text, *, field="after"):
        """Local future-message edit; never arm, send, or rewrite an order snapshot."""
        from app.services.delivery_chains import DeliveryChains

        completion = field == "completion"
        text = text if completion and type(text) is bool else message_text(text)
        if field not in {"greeting", "after", "completion"}:
            raise Conflict("Поле сообщения недоступно")
        with self.store.transaction() as db:
            product = self.listing(item_id)
            if not product:
                raise Conflict("Сначала подтвердите привязку товара")
            self.authority(owner, product["account_id"])
            if product["owner_id"] != owner or any(
                product[key] != expected.get(key) for key in ("revision", "control_revision", "config")
            ):
                raise Conflict("Настройки или остановка изменились во время ввода. Откройте сообщение заново")
            config = json.loads(product["config"])
            platform = config.get("platform_only") is True
            if (
                product["state"] != "VERIFIED"
                or not product["verified_at"]
                or product["handler"] == "manual"
                and not platform
                or platform
                and field not in {"greeting", "completion"}
                or not platform
                and product["handler"] == "telegram_gifts"
                and field not in {"after", "completion"}
            ):
                raise Conflict("Сообщение доступно после подтверждения автоматического способа выдачи")
            aliases = db.execute(
                "SELECT item_id FROM logical_listings WHERE product_id=? AND config_revision=? AND verified_at IS NOT NULL",
                (product["id"], product["revision"]),
            ).fetchall()
            for alias in aliases:
                target = alias["item_id"]
                revision, policy = Automation(self.store, self.api).get(target)
                if db.execute(
                    "SELECT 1 FROM outbox WHERE target=? AND kind IN ('edit','edit_fields','delete','publish','premium_publish') "
                    "AND status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') LIMIT 1",
                    (target,),
                ).fetchone():
                    raise Conflict("Изменение или публикация товара не завершены. Сначала сверьте операцию")
                if product["enabled"]:
                    for order in db.execute(
                        "SELECT id,payload FROM orders WHERE item_id=? AND (status='PAID' OR ? AND status IN ('SENT','CONFIRMED','CONFIRMED_AUTOMATICALLY')) "
                        "AND NOT EXISTS(SELECT 1 FROM logical_order_scopes s WHERE s.order_id=orders.id)",
                        (target, platform),
                    ).fetchall():
                        if future_order(json.loads(order["payload"]).get("created_at"), product["armed_at"]):
                            self.pin_order(
                                db, order["id"], target, "playerok" if platform else product["handler"]
                            )
                            if platform:
                                db.execute(
                                    "UPDATE logical_order_scopes SET config=json_set(config,'$.greeting',?,'$.mark_sent',json(?)) WHERE order_id=?",
                                    (policy.greeting, encode(policy.mark_sent), order["id"]),
                                )
                if policy.mode in {"text", "stock", "playerok"}:
                    DeliveryChains(self.store, self.api).freeze_stock_resume(db, target, revision, policy)
                if completion:
                    for scope in db.execute(
                        "SELECT s.order_id,s.config,c.policy FROM logical_order_scopes s "
                        "JOIN orders o ON o.id=s.order_id LEFT JOIN delivery_chains c ON c.order_id=s.order_id "
                        "WHERE s.product_id=? AND s.item_id=? AND o.status IN ('PAID','SENT')",
                        (product["id"], target),
                    ).fetchall():
                        saved = json.loads(scope["config"])
                        prior = dict(saved)
                        if product["handler"] == "telegram_gifts":
                            listing = db.execute(
                                "SELECT auto_fulfill FROM gift_listings WHERE item_id=?", (target,)
                            ).fetchone()
                            saved.setdefault("auto_fulfill", bool(listing and listing[0]))
                        else:
                            original = json.loads(scope["policy"]) if scope["policy"] else asdict(policy)
                            saved.setdefault("mark_sent", original.get("mark_sent", False))
                            saved.setdefault(
                                "automatic_completion", original.get("automatic_completion", False)
                            )
                        if saved != prior:
                            db.execute(
                                "UPDATE logical_order_scopes SET config=? WHERE order_id=?",
                                (encode(saved), scope["order_id"]),
                            )
                if policy.mode in {"text", "stock", "playerok"}:
                    updated = asdict(policy)
                    if completion:
                        updated["mark_sent"] = text
                        if policy.mode != "playerok":
                            updated["automatic_completion"] = text
                    else:
                        updated[field] = text
                    db.execute(
                        "UPDATE delivery_policies SET payload=?,revision=revision+? WHERE item_id=? AND revision=?",
                        (encode(updated), int(policy.mode != "playerok"), target, revision),
                    )
                if product["handler"] == "telegram_gifts":
                    if completion:
                        db.execute(
                            "UPDATE gift_listings SET auto_fulfill=? WHERE item_id=?", (int(text), target)
                        )
                    else:
                        self.store.set_setting(
                            "gifts:listing_options:" + target,
                            {**self.store.setting("gifts:listing_options:" + target, {}), "after": text},
                        )
            if completion:
                if product["handler"] == "telegram_gifts":
                    config["auto_fulfill"] = text
                else:
                    config["mark_sent"] = text
                    config["automatic_completion"] = text
            else:
                config[field] = text
            db.execute(
                "UPDATE logical_products SET config=?,updated_at=? WHERE id=?",
                (encode(config), now(), product["id"]),
            )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,?,?,?,?)",
                (
                    owner,
                    "UPDATE_FUTURE_COMPLETION" if completion else "UPDATE_FUTURE_MESSAGE",
                    product["id"],
                    digest(encode(without_messages(config))) if completion else digest(str(text)),
                    now(),
                ),
            )

    def same_delivery_config(self, before, binding):
        if without_messages(before) == without_messages(binding["config"]):
            return True
        return bool(
            without_messages(before, completion=True) == without_messages(binding["config"], completion=True)
            and self.store.rows(
                "SELECT 1 FROM audit_log WHERE actor=? AND action='UPDATE_FUTURE_COMPLETION' AND target=? AND outcome=? LIMIT 1",
                (binding["owner_id"], binding["id"], digest(encode(without_messages(binding["config"])))),
            )
        )

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
            "SELECT p.*,l.item_id,l.config_revision,l.description_sha256,l.server_status,l.verified_at,l.operation_id,l.source_order_id,l.title_snapshot "
            "FROM logical_listings l JOIN logical_products p ON p.id=l.product_id WHERE l.item_id=?",
            (item_id,),
        )
        return rows[0] if rows else None

    def overview(self, owner, *, handler=None, limit=11, offset=0):
        """One saved public representative per product, plus unlinked old settings."""
        if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (owner,)):
            raise Conflict("Доступ закрыт")
        account = self.store.setting("playerok:account_id")
        if self.api and account and account != self.api.account.id:
            raise Conflict("Аккаунт не соответствует привязке базы")
        return self.store.rows(
            "WITH ranked AS (SELECT l.item_id,CASE WHEN json_extract(p.config,'$.platform_only')=1 "
            "THEN 'playerok' ELSE p.handler END handler,p.config,p.updated_at,"
            "row_number() OVER (PARTITION BY p.id ORDER BY "
            "(l.server_status='APPROVED' AND l.verified_at IS NOT NULL AND l.config_revision=p.revision) DESC,"
            "l.verified_at DESC,l.item_id) position FROM logical_products p "
            "JOIN logical_listings l ON l.product_id=p.id WHERE p.owner_id=? AND p.account_id=? "
            "AND l.source_order_id IS NULL),"
            "entries AS (SELECT item_id,handler,config,updated_at,json_extract(config,'$.bundle') bundle,"
            "(SELECT sum(json_extract(value,'$.stars')) FROM json_each(config,'$.bundle')) approved_stars "
            "FROM ranked WHERE position=1 UNION ALL "
            "SELECT d.item_id,CASE WHEN g.item_id IS NOT NULL THEN 'telegram_gifts' "
            "WHEN json_extract(d.payload,'$.mode')='stock' THEN 'unique_stock' "
            "WHEN json_extract(d.payload,'$.mode')='text' THEN 'fixed_text' ELSE 'manual' END handler,"
            "NULL,d.updated_at,g.bundle,g.approved_stars FROM delivery_policies d "
            "LEFT JOIN gift_listings g ON g.item_id=d.item_id AND g.owner_id=d.actor WHERE d.actor=? "
            "AND NOT EXISTS(SELECT 1 FROM logical_listings l WHERE l.item_id=d.item_id) UNION ALL "
            "SELECT g.item_id,'telegram_gifts',NULL,'',g.bundle,g.approved_stars FROM gift_listings g WHERE g.owner_id=? "
            "AND NOT EXISTS(SELECT 1 FROM logical_listings l WHERE l.item_id=g.item_id) "
            "AND NOT EXISTS(SELECT 1 FROM delivery_policies d WHERE d.item_id=g.item_id)) "
            "SELECT item_id,handler,config,coalesce(bundle,'[]') bundle,coalesce(approved_stars,0) approved_stars "
            "FROM entries WHERE (? IS NULL OR handler=?) ORDER BY updated_at DESC,item_id LIMIT ? OFFSET ?",
            (owner, account, owner, owner, handler, handler, limit, offset),
        )

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
        if not listing:
            return None
        if listing["state"] == "LEGACY" or len(listing["code"]) != 3:
            raise Conflict("Требуется перенос постоянного кода в название товара")
        if listing["handler"] != "telegram_gifts":
            raise Conflict("Постоянный код или настройки подарочного товара изменились")
        self.verify_listing(listing, item)
        return json.loads(listing["config"])["code"]

    def migrate_legacy(self, item_id=None):
        """Local projection only: leave old bindings/intents/permissions unchanged."""
        account = self.store.setting("playerok:account_id")
        if not account:
            return 0
        imported = 0
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT g.* FROM gift_listings g WHERE NOT EXISTS "
                "(SELECT 1 FROM logical_listings l WHERE l.item_id=g.item_id) AND (? IS NULL OR g.item_id=?)",
                (item_id, item_id),
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
                code = reserve_code(db, ident)
                db.execute(
                    "INSERT INTO logical_products(id,code,owner_id,account_id,handler,config,state,stock_item_id,baseline,original_description,created_at,updated_at) "
                    "VALUES(?,?,?,?,'telegram_gifts',?,'LEGACY',?,'{}','',?,?)",
                    (ident, code, row["owner_id"], account, encode(config), row["item_id"], now(), now()),
                )
                db.execute(
                    "INSERT INTO logical_listings(item_id,product_id,config_revision,description_sha256,server_status) VALUES(?,?,1,?,'UNVERIFIED')",
                    (row["item_id"], ident, row["description_sha256"]),
                )
                imported += 1
        return imported

    async def prepare_creation(self, owner, value):
        """Read-only preparation for a new wizard-created private draft."""
        account = self.authority(owner, writes=True)
        handler = {
            "gifts": "telegram_gifts",
            "text": "fixed_text",
            "stock": "unique_stock",
            "playerok": "manual",
            "manual": "manual",
        }.get(value.get("delivery"))
        if not handler:
            raise Conflict("Выберите способ выдачи")
        if marker(value["name"]):
            raise Conflict("Новый товар не может присвоить существующий постоянный код")
        gifts = self.gift_service() if handler == "telegram_gifts" else None
        config = configuration(
            handler,
            {
                "code": value.get("gift_code", ""),
                **({"text": value.get("delivery_data", "")} if handler == "fixed_text" else {}),
                "after": "",
                "greeting": "",
                **({"platform_only": True} if value.get("delivery") == "playerok" else {}),
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
        ident = uuid.uuid4().hex
        code = reserve_code(db, ident)
        original = value["description"]
        value = {
            **value,
            "managed_product_id": ident,
            "permanent_code": code,
            "name": listing_text(marked_title(value["name"], code), "name"),
            "description": original,
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
            marker(observed.get("name")) != product["code"]
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
        db.execute(
            "UPDATE logical_listings SET title_snapshot=? WHERE item_id=? AND product_id=?",
            (marked_title(value["name"], product["code"]), proof["remote"], product["id"]),
        )
        db.execute("UPDATE drafts SET state='REMOTE_DRAFT' WHERE id=?", (draft_id,))
        if value.get("cost") is not None:
            self.store.set_setting(f"finance:{owner}:cost:{proof['remote']}", value["cost"])

    async def prepare_legacy_description(self, owner, item_id):
        existing = self.listing(item_id)
        if not existing or existing["state"] != "LEGACY" or existing["handler"] != "telegram_gifts":
            raise Conflict("Перенос недоступен. Обновите карточку привязки")
        return await self.prepare(owner, item_id, existing["handler"], {}, migrate_legacy=True)

    async def prepare(self, owner, item_id, handler, config, *, migrate_legacy=False, platform_binding=False):
        account = self.authority(owner)
        item = await self.api.call("get_item", id=item_id)
        baseline = critical(item)
        if item.get("id") != item_id or baseline["account"] != account:
            raise Conflict("Товар принадлежит другому аккаунту")
        if item.get("is_editable") is False:
            raise Conflict("Playerok запрещает редактирование этого товара")
        allowed = {"DRAFT", "APPROVED"}
        if platform_binding:
            from app.services.platform_orders import PLATFORM_STATUSES

            allowed = PLATFORM_STATUSES
            if item.get("status") not in {"DRAFT", "APPROVED"} and item.get("is_editable") is not True:
                raise Conflict(
                    "Playerok не подтвердил разрешение редактировать архивный товар. Откройте его на сайте"
                )
        if item.get("deleted_at") or item.get("status") not in allowed:
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
                or not 0 < order_limit <= global_order
                or (
                    day_limit is not None
                    and (
                        type(day_limit) is not int
                        or not order_limit <= day_limit <= (global_day or gifts.settings.max_daily_stars)
                    )
                )
            ):
                raise Conflict(
                    "Лимиты товара должны быть положительными и не превышать общие лимиты магазина"
                )
            if total > order_limit or (global_day is not None and total > (day_limit or global_day)):
                raise Conflict("Стоимость пакета превышает выбранный лимит Stars")
            refund = options.get("refund", False)
            if type(refund) is not bool:
                raise Conflict("Возврат для товара требует явного выбора: включён или выключен")
            config["options"] = {"order": order_limit, "day": day_limit, "refund": refund}
            if total > balance:
                raise Conflict("Недостаточно Stars для настроенного пакета подарков")
        description = item.get("description")
        existing_marker = marker(item.get("name"))
        if existing and len(existing["code"]) != 3:
            raise Conflict("Требуется перенос кода товара. Откройте перенос идентификаторов")
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
            with self.store.lock:
                code = choose_code(self.store.db)
        name = listing_text(marked_title(item["name"], code), "name")
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
            "name": name,
            "original_name": item["name"],
            "status": item["status"],
            "product_id": existing["id"] if existing else None,
            "revision": existing["revision"] if existing else 0,
            "control_revision": existing["control_revision"] if existing else 0,
            "source_config": existing["config"] if existing else None,
            "code": code,
            **({"platform_binding": True} if platform_binding else {}),
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
                    or current_link["config"] != prepared.get("source_config")
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
                code = reserve_code(db, ident, prepared["code"])
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
            description = listing_text(prepared["cleaned"], "description")
            name = listing_text(prepared["name"], "name")
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
                    "name": name,
                    "logical_binding": {
                        "product_id": ident,
                        "revision": revision,
                        "account": prepared["account"],
                        "original_hash": digest(prepared["original"]),
                        "original_name": prepared["original_name"],
                        "baseline": prepared["baseline"],
                        **({"platform_binding": True} if prepared.get("platform_binding") else {}),
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
            or current.get("name") != payload.get("original_name")
            or digest(current.get("description", "")) != payload["original_hash"]
        ):
            raise Conflict("Товар изменился после превью. Обновите данные и подтвердите заново")

    def confirm_binding(self, action, observed, *, writes=True):
        self.binding_guard(action, writes=writes)
        expected = action["payload"]["description"]
        product = self.product(action["payload"]["logical_binding"]["product_id"])
        if len(product["code"]) == 8 and not writes:
            # Historical read-only resolution only. This never authorizes a new
            # purchase/delivery, and the product still requires title migration.
            from app.services.product_codes import remove_legacy_tail

            row = self.store.rows("SELECT status FROM outbox WHERE id=?", (action["id"],))
            if (
                not row
                or row[0]["status"] != "UNCERTAIN"
                or remove_legacy_tail(expected, product["code"]) == expected
            ):
                raise Conflict("Не подтверждена историческая неизвестная операция")
            if (
                observed.get("id") != action["target"]
                or critical(observed) != action["payload"]["logical_binding"]["baseline"]
                or observed.get("description") != expected
            ):
                raise Conflict("Историческая запись не подтверждена сервером")
            return {
                "product_id": product["id"],
                "revision": product["revision"],
                "item_id": action["target"],
                "status": observed["status"],
                "description_sha256": digest(expected),
                "baseline": critical(observed),
                "title_snapshot": observed["name"],
            }
        if action["payload"]["logical_binding"].get("platform_binding"):
            from app.services.platform_orders import PLATFORM_STATUSES

            if observed.get("deleted_at") or observed.get("status") not in PLATFORM_STATUSES:
                raise Conflict("Состояние товара больше не разрешает подтверждение привязки")
        if (
            observed.get("id") != action["target"]
            or critical(observed)
            != {**action["payload"]["logical_binding"]["baseline"], "name": product["code"]}
            or observed.get("description") != expected
            or marker(observed.get("name")) != product["code"]
        ):
            raise Conflict("Playerok не подтвердил точное описание и владельца привязки")
        return {
            "product_id": product["id"],
            "revision": product["revision"],
            "item_id": action["target"],
            "status": observed["status"],
            "description_sha256": digest(expected),
            "baseline": critical(observed),
            "title_snapshot": marked_title(observed["name"], product["code"]),
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
        return self._verify_listing(product, item, check_description=True)

    def verify_marketing_description(self, product, item, *, operation_id=None):
        if (
            not product
            or item.get("id") != product["item_id"]
            or product["source_order_id"]
            or product["error_code"]
            or item.get("status") != "APPROVED"
            or item.get("is_editable") is False
            or item.get("deleted_at")
            or not isinstance(item.get("description"), str)
        ):
            raise Conflict("Безопасное редактирование описания этого объявления не подтверждено")
        self.authority(product["owner_id"], product["account_id"], writes=True)
        self._verify_listing(product, item, check_description=True, operation_id=operation_id)

    def _verify_listing(self, product, item, *, check_description, operation_id=None):
        if self.store.rows(
            "SELECT 1 FROM outbox WHERE target=? AND id!=COALESCE(?, '') "
            "AND status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') "
            "AND (kind='edit_fields' OR kind='edit' AND (?=0 OR json_type(payload,'$.description') IS NOT NULL "
            "AND json_extract(payload,'$.logical_binding') IS NULL "
            "AND json_extract(payload,'$.code_migration') IS NULL "
            "AND (json_extract(payload,'$.marketing_description') IS NULL OR status='UNCERTAIN'))) LIMIT 1",
            (item.get("id"), operation_id, int(check_description)),
        ):
            raise Conflict(
                "Редактирование товара не завершено. Сначала сверьте операцию, затем подтвердите привязку"
            )
        link = self.listing(item.get("id"))
        actual = critical(item)
        baseline = json.loads(product["baseline"])
        # RUB sale price is not delivery identity or the approved Stars cost.
        # Keep the original snapshot for audit; callers obtain fresh server data.
        for key, label in {
            "account": "владелец",
            "category": "категория",
            "obtaining": "способ получения",
            "name": "постоянный код",
            "fields": "характеристики или поля получения",
        }.items():
            if actual.get(key) != baseline.get(key):
                raise Conflict("Изменились " + label + ". Проверьте настройки и подтвердите привязку заново")
        if link and marked_title(item["name"], product["code"]) != link["title_snapshot"]:
            raise Conflict("Изменилось название товара. Проверьте настройки и подтвердите привязку заново")
        if check_description and link and digest(item["description"]) != link["description_sha256"]:
            raise Conflict("Изменилось описание товара. Проверьте настройки и подтвердите привязку заново")
        if (
            product["state"] != "VERIFIED"
            or len(product["code"]) != 3
            or not link
            or link["id"] != product["id"]
            or not link["verified_at"]
            or link["config_revision"] != product["revision"]
            or marker(item.get("name")) != product["code"]
            or marked_title(item["name"], product["code"]) != link["title_snapshot"]
            or check_description
            and digest(item["description"]) != link["description_sha256"]
        ):
            raise Conflict("Код, описание или критичные параметры товара не подтверждены")

    def verify_title_change(self, product, item):
        """Narrow exception for an explicitly confirmed title, never delivery authority."""
        if (
            not product
            or product["source_order_id"]
            or product["error_code"] not in {None, "RELIST_CONFIRMATION_REQUIRED"}
            or not product["title_snapshot"]
            or item.get("id") != product["item_id"]
            or marker(item.get("name")) != product["code"]
            or item.get("deleted_at")
            or item.get("status") not in {"APPROVED", "DRAFT", "SOLD"}
        ):
            raise Conflict("Изменение не ограничивается названием подтверждённого товара")
        if self.store.rows(
            "SELECT 1 FROM outbox WHERE target=? AND kind IN ('edit','edit_fields') "
            "AND status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') LIMIT 1",
            (product["item_id"],),
        ):
            raise Conflict("Сначала сверьте незавершённую правку. Повторная запись не выполняется")
        # Validate the real marker above before substituting ONLY the old title.
        # All other current server fields still pass the original binding guard.
        self.verify_listing(product, {**item, "name": product["title_snapshot"]})

    def title_change_allowed(self, product, item):
        try:
            self.verify_title_change(product, item)
        except (ValueError, KeyError, TypeError):
            return False
        return item["name"] != product["title_snapshot"]

    async def prepare_title_change(self, owner, item_id):
        account = self.authority(owner)
        product = self.listing(item_id)
        if not product or product["owner_id"] != owner or product["account_id"] != account:
            raise Conflict("Привязка товара принадлежит другому владельцу")
        item = await self.api.call("get_item", id=item_id)
        return self.title_evidence(owner, product, item)

    def title_evidence(self, owner, product, item):
        self.authority(owner, product["account_id"])
        self.verify_title_change(product, item)
        return {
            "owner": owner,
            "item": item["id"],
            "product": digest(encode(product)),
            "old_title": product["title_snapshot"],
            "title": item["name"],
            "evidence": digest(
                encode(
                    {"critical": critical(item), "description": item["description"], "status": item["status"]}
                )
            ),
        }

    async def refresh_title(self, item):
        """Standing permission for verified marketing text; never arm or spend."""
        product = self.listing(item.get("id"))
        if not product or not isinstance(item.get("description"), str):
            return False
        description_changed = digest(item["description"]) != product["description_sha256"]
        if product["title_snapshot"] == item.get("name") and not description_changed:
            return False
        locks = getattr(self.store, "logical_title_locks", None)
        if locks is None:
            self.store.logical_title_locks = locks = {}
        key = (self.api.account.id, item.get("id"))
        async with locks.setdefault(key, asyncio.Lock()):
            product = self.listing(item.get("id"))
            if digest(item["description"]) != product["description_sha256"]:
                return await self._refresh_description(product, item)
            if not self.title_change_allowed(product, item):
                return False
            prepared = self.title_evidence(product["owner_id"], product, item)
            await self.confirm_title_change(product["owner_id"], prepared, automatic=True)
            return True

    async def _refresh_description(self, product, item):
        # Description is marketing text, not delivery content. Never infer a
        # new Gift, stock unit or recipient from it; preserve the approved config.
        if self.store.rows(
            "SELECT 1 FROM outbox WHERE target=? AND kind='edit' AND status='PROCESSING' "
            "AND started_at IS NOT NULL AND json_extract(payload,'$.marketing_description') IS NOT NULL",
            (item["id"],),
        ):
            raise APIError("MARKETING_EDIT_READBACK_PENDING")
        if (
            product["state"] != "VERIFIED"
            or not product["verified_at"]
            or product["config_revision"] != product["revision"]
            or product["source_order_id"]
            or product["error_code"] not in {None, "RELIST_CONFIRMATION_REQUIRED"}
            or marker(item.get("name")) != product["code"]
            or item.get("deleted_at")
            or item.get("status") not in {"APPROVED", "DRAFT", "SOLD"}
        ):
            return False
        self.authority(product["owner_id"], product["account_id"])
        try:
            self._verify_listing(
                product, {**item, "name": product["title_snapshot"]}, check_description=False
            )
        except (ValueError, KeyError, TypeError):
            return False  # Preserve the existing critical-reconfiguration route.
        proof = digest(encode(product))
        expected = {key: item.get(key) for key in ("id", "name", "description", "status", "deleted_at")}
        identity = critical(item)
        for _ in range(2):
            fresh = await self.api.call("get_item", id=item["id"])
            if {key: fresh.get(key) for key in expected} != expected or critical(fresh) != identity:
                raise Conflict("Объявление изменилось во время фоновой проверки; проверка будет повторена")
        with self.store.transaction() as db:
            current = self.listing(item["id"])
            if not current or digest(encode(current)) != proof:
                raise Conflict("Настройки или остановка изменились во время фоновой проверки")
            self.authority(product["owner_id"], product["account_id"])
            self._verify_listing(
                current, {**fresh, "name": current["title_snapshot"]}, check_description=False
            )
            config = json.loads(current["config"])
            gift = db.execute("SELECT * FROM gift_listings WHERE item_id=?", (item["id"],)).fetchone()
            if current["handler"] == "telegram_gifts" and gift:
                if (
                    gift["owner_id"] != current["owner_id"]
                    or gift["gift_code"] != config["code"]
                    or json.loads(gift["bundle"]) != config["bundle"]
                    or gift["category_id"] != identity["category"]
                    or gift["obtaining_type_id"] != identity["obtaining"]
                    or gift["description_sha256"] != current["description_sha256"]
                ):
                    raise Conflict("Подарочная настройка не совпадает с подтверждённым товаром")
                db.execute(
                    "UPDATE gift_listings SET description_sha256=? WHERE item_id=?",
                    (digest(fresh["description"]), item["id"]),
                )
            db.execute(
                "UPDATE logical_listings SET title_snapshot=?,description_sha256=?,verified_at=? "
                "WHERE item_id=? AND product_id=? AND config_revision=?",
                (
                    fresh["name"],
                    digest(fresh["description"]),
                    now(),
                    item["id"],
                    product["id"],
                    product["revision"],
                ),
            )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,?,?,?,?)",
                (
                    product["owner_id"],
                    "REFRESH_LISTING_METADATA",
                    item["id"],
                    digest(fresh["description"]),
                    now(),
                ),
            )
        return True

    async def confirm_title_change(self, owner, prepared, *, automatic=False):
        if prepared.get("owner") != owner:
            raise Conflict("Подтверждение принадлежит другому владельцу")
        observed = await self.prepare_title_change(owner, prepared["item"])
        if observed != prepared:
            raise Conflict("Товар, настройки или остановка изменились. Проверьте новое название заново")
        with self.store.transaction() as db:
            product = self.listing(prepared["item"])
            if digest(encode(product)) != prepared["product"]:
                raise Conflict("Настройки изменились во время проверки. Откройте товар заново")
            self.authority(owner, product["account_id"])
            # Recheck local pending mutations inside the same SQLite transaction.
            if db.execute(
                "SELECT 1 FROM outbox WHERE target=? AND kind IN ('edit','edit_fields') "
                "AND status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') LIMIT 1",
                (prepared["item"],),
            ).fetchone():
                raise Conflict("Есть незавершённая правка. Сначала сверьте её результат")
            db.execute(
                "UPDATE logical_listings SET title_snapshot=? WHERE item_id=? AND product_id=?",
                (prepared["title"], prepared["item"], product["id"]),
            )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,?,?,?,?)",
                (
                    owner,
                    "REFRESH_LISTING_TITLE" if automatic else "CONFIRM_LISTING_TITLE",
                    prepared["item"],
                    digest(prepared["title"]),
                    now(),
                ),
            )

    async def arm(self, owner, product_id, revision, *, inherit=False, item_id=None):
        product = self.product(product_id, owner)
        if json.loads(product["config"]).get("platform_only"):
            raise Conflict("Товар выдаёт Playerok. Включите нужные действия в настройках этого режима")
        self.authority(owner, product["account_id"], writes=True)
        if not getattr(self.api.account, "automation_enabled", False):
            raise Conflict("Автоматизация выключена в настройках подключения")
        if product["revision"] != revision or product["handler"] == "manual":
            raise Conflict("Настройки изменились, выдача уже включена или выбран ручной режим")
        ident = item_id or product["stock_item_id"]
        item = await self.api.call("get_item", id=ident)
        if item.get("id") != ident:
            raise Conflict("Playerok вернул другое объявление. Обновите карточку товара")
        self.verify_listing(product, item)
        if item.get("status") != "APPROVED":
            raise Conflict("Включение доступно для опубликованного товара")
        config = json.loads(product["config"])
        arm_context = {
            "product_id": product_id,
            "revision": revision,
            "control_revision": product["control_revision"],
            "inherit": bool(inherit),
            "expected_enabled": product["enabled"],
            "source_config": product["config"],
        }
        if product["handler"] == "telegram_gifts":
            gifts = self.gift_service()
            listing = gifts.listing(item["id"])
            if listing and listing["enabled"]:
                if (
                    listing["gift_code"] != config["code"]
                    or json.loads(listing["bundle"]) != config["bundle"]
                ):
                    raise Conflict(
                        "Состав подарков расходится с привязкой. Остановите выдачу и подтвердите настройки"
                    )
                if product["enabled"]:
                    return await self._active_inheritance(owner, product, item, inherit, listing)
            else:
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
                automatic_completion=config.get("automatic_completion", False),
            )
            automation = Automation(self.store, self.api)
            old_revision, old_policy = automation.get(item["id"])
            if product["enabled"] and old_policy.enabled:
                if old_policy != policy:
                    raise Conflict("Политика выдачи расходится с подтверждёнными настройками")
                projection = self.store.rows("SELECT * FROM delivery_policies WHERE item_id=?", (ident,))[0]
                return await self._active_inheritance(owner, product, item, inherit, projection)
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

    async def _active_inheritance(self, owner, product, item, inherit, projection):
        if bool(product["inherit_enabled"]) == bool(inherit):
            raise Conflict("Выдача уже включена. Обновите карточку")
        fresh = await self.api.call("get_item", id=item["id"])
        self.verify_listing(product, fresh)
        if critical(fresh) != critical(item) or any(
            fresh.get(k) != item.get(k) for k in ("id", "name", "description", "status", "deleted_at")
        ):
            raise Conflict("Объявление изменилось во время настройки наследования")
        table = "gift_listings" if product["handler"] == "telegram_gifts" else "delivery_policies"
        with self.store.transaction() as db:
            self.authority(owner, product["account_id"], writes=True)
            current = self.product(product["id"], owner)
            rows = self.store.rows(f"SELECT * FROM {table} WHERE item_id=?", (item["id"],))
            if (
                current != product
                or rows != [projection]
                or not getattr(self.api.account, "automation_enabled", False)
            ):
                raise Conflict("Настройки или остановка изменились; наследование не изменено")
            self.verify_listing(current, fresh)
            db.execute(
                "UPDATE logical_products SET inherit_enabled=?,updated_at=? WHERE id=?",
                (int(bool(inherit)), now(), product["id"]),
            )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,?,?,?,?)",
                (owner, "SET_NATIVE_INHERITANCE", product["id"], str(bool(inherit)), now()),
            )
        return product["armed_at"]

    async def resume_stock(self, owner, item_id, expected, policy_revision, values):
        """Restore admission after replenishment without changing paid-order authority."""
        product = self.listing(item_id)
        if not product or any(
            product[key] != expected.get(key)
            for key in ("id", "revision", "control_revision", "config", "enabled", "inherit_enabled")
        ):
            raise Conflict("Настройки или остановка изменились. Откройте склад заново")
        self.authority(owner, product["account_id"], writes=True)
        self.require_delivery(item_id, "unique_stock")
        if product["owner_id"] != owner or not self.api.account.automation_enabled:
            raise Conflict("Автоматизация выключена или владелец не подтверждён")
        automation = Automation(self.store, self.api)
        for alias in self.store.rows(
            "SELECT item_id FROM logical_listings WHERE product_id=? AND source_order_id IS NULL "
            "AND (server_status='APPROVED' OR item_id=?)",
            (product["id"], item_id),
        ):
            item = await automation.item(owner, alias["item_id"])
            self.verify_listing(product, item)
            if alias["item_id"] != item_id and item.get("status") == "APPROVED":
                raise Conflict("С одним кодом опубликовано несколько товаров. Сначала разрешите конфликт")
        context = {
            "product_id": product["id"],
            "revision": product["revision"],
            "control_revision": product["control_revision"],
            "expected_enabled": product["enabled"],
            "source_config": product["config"],
            "inherit": bool(product["inherit_enabled"]),
            "stock_resume": True,
        }
        return await automation.save(
            owner,
            item_id,
            policy_revision,
            values,
            future_confirmed=True,
            stock_resume=True,
            logical_arm=context,
        )

    def commit_arm(self, db, owner, item_id, context, cutoff):
        """Same transaction as the existing execution policy: STOP always fences late arm."""
        product = self.product(context["product_id"], owner)
        link = self.listing(item_id)
        self.authority(owner, product["account_id"], writes=True)
        if (
            not getattr(self.api.account, "automation_enabled", False)
            or product["revision"] != context["revision"]
            or product["control_revision"] != context["control_revision"]
            or product["enabled"] != context.get("expected_enabled", 0)
            or product["state"] != "VERIFIED"
            or product["config"] != context.get("source_config")
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
        if context.get("stock_resume"):
            if product["handler"] != "unique_stock" or not product["enabled"]:
                raise Conflict("Возобновить можно только разрешённый складской товар")
            # This is admission, not STOP/re-arm: existing reservations retain
            # their control fence and immutable order snapshots.
            return
        changed = db.execute(
            "UPDATE logical_products SET enabled=1,inherit_enabled=?,armed_at=?,updated_at=?,control_revision=control_revision+? WHERE id=? AND revision=? AND control_revision=? AND enabled=?",
            (
                int(context["inherit"]),
                cutoff,
                now(),
                int(bool(context.get("expected_enabled", 0))),
                product["id"],
                context["revision"],
                context["control_revision"],
                context.get("expected_enabled", 0),
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
        expected_product=None,
    ):
        """Called only with a completed account scan; never discover or spend here."""
        product = self.product(product_id, owner)
        if expected_product and any(
            product[key] != expected_product[key]
            for key in ("revision", "control_revision", "state", "enabled", "inherit_enabled", "config")
        ):
            raise RelistConflict("RELIST_CONTROL_CHANGED")
        if not self.api.account.writes_enabled and getattr(
            getattr(self.api, "connection", None), "awaiting_verification", False
        ):
            self.authority(owner, product["account_id"])
            raise APIError("CONNECTION_NOT_VERIFIED")
        self.authority(owner, product["account_id"], writes=True)
        existing = self.listing(item.get("id"))
        if existing:
            await self.refresh_title(item)
            self.verify_listing(product, item)
        if (
            product["state"] != "VERIFIED"
            or not existing
            and (not product["enabled"] or not product["inherit_enabled"])
        ):
            raise RelistConflict("RELIST_PERMISSION_MISSING")
        if (
            not existing
            and {k: v for k, v in critical(item).items() if k != "price"}
            != {k: v for k, v in json.loads(product["baseline"]).items() if k != "price"}
            or marker(item.get("name")) != product["code"]
            or item.get("status") not in ({"APPROVED", "SOLD"} if paid_order else {"APPROVED"})
            or item.get("deleted_at")
        ):
            raise RelistConflict("RELIST_CRITICAL_FIELDS_CHANGED")
        if source_order_id and (
            not paid_order
            or paid_order.get("id") != source_order_id
            or product["handler"] not in {"telegram_gifts", "fixed_text", "unique_stock"}
            and not json.loads(product["config"]).get("platform_only")
        ):
            raise RelistConflict("RELIST_CANDIDATE_INVALID")
        active = []
        for observed in complete_snapshot:
            if (observed.get("user") or {}).get("id") != product["account_id"]:
                raise RelistConflict("RELIST_CANDIDATE_INVALID")
            if (
                observed.get("id") not in snapshot_item_ids
                and observed.get("status") == "APPROVED"
                and not observed.get("deleted_at")
                and marker(observed.get("name")) == product["code"]
            ):
                active.append(observed["id"])
        if paid_order is not None:
            from app.services.order_management import deliverable

            if json.loads(product["config"]).get("platform_only"):
                from app.services.platform_orders import platform_sale

                platform_sale(paid_order, paid_order.get("id"), product["account_id"])
            else:
                deliverable(paid_order, paid_order.get("id"), product["account_id"])
            if (paid_order.get("item") or {}).get("id") != item["id"] or not future_order(
                paid_order.get("created_at"), product["armed_at"]
            ):
                raise RelistConflict("RELIST_CANDIDATE_INVALID")
        if (not source_order_id and item.get("status") == "APPROVED" and set(active) != {item["id"]}) or len(
            set(active)
        ) > 1:
            raise RelistConflict("DUPLICATE_ACTIVE_MARKER")
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
        if not links:
            raise RelistConflict("RELIST_SOURCE_UNVERIFIED")
        if not isinstance(item.get("description"), str):
            raise Conflict("Описание нового объявления не получено; ожидаем полную серверную карточку")
        if source_order_id and (
            any(link["description_sha256"] != digest(item["description"]) for link in links)
            or not any(
                link["title_snapshot"] == marked_title(item["name"], product["code"]) for link in links
            )
        ):
            # A private purchased snapshot cannot inherit a later marketing
            # edit of the public listing. Preserve its original identity proof.
            raise RelistConflict("RELIST_BINDING_CONFLICT")
        if any(link["description_sha256"] != digest(item["description"]) for link in links) or not any(
            link["title_snapshot"] == marked_title(item["name"], product["code"]) for link in links
        ):
            if source_order_id:
                from app.services.order_management import compatible_detail

                detail = await self.api.call("get_delivery_deal", deal_id=source_order_id)
                compatible_detail(paid_order, detail)
                fresh = detail.get("item") or {}
            else:
                fresh = await self.api.call("get_item", id=item["id"])
            if critical(fresh) != critical(item) or any(
                fresh.get(k) != item.get(k) for k in ("id", "name", "description", "status", "deleted_at")
            ):
                raise RelistConflict("RELIST_ITEM_CHANGED")
        config = json.loads(product["config"])
        platform = config.get("platform_only") is True
        if product["handler"] == "telegram_gifts" and not platform:
            gifts = self.gift_service()
            await gifts.refresh()
            if gifts.catalog_bundle(config["code"]) != config["bundle"]:
                raise RelistConflict("RELIST_GIFT_CONFIGURATION_CHANGED")
        with self.store.transaction() as db:
            current = self.product(product_id)
            if (
                not current["enabled"]
                or current["revision"] != product["revision"]
                or current["control_revision"] != product["control_revision"]
                or current["armed_at"] != product["armed_at"]
                or current["config"] != product["config"]
                or not current["inherit_enabled"]
            ):
                raise RelistConflict("RELIST_CONTROL_CHANGED")
            if not self.api.account.writes_enabled and getattr(
                getattr(self.api, "connection", None), "awaiting_verification", False
            ):
                self.authority(owner, product["account_id"])
                raise APIError("CONNECTION_NOT_VERIFIED")
            self.authority(owner, product["account_id"], writes=True)
            if source_order_id:
                from app.services.order_management import deliverable

                row = db.execute("SELECT payload FROM orders WHERE id=?", (source_order_id,)).fetchone()
                if not row:
                    raise RelistConflict("RELIST_CANDIDATE_INVALID")
                local_order = json.loads(row[0])
                if platform:
                    from app.services.platform_orders import platform_sale

                    platform_sale(local_order, source_order_id, product["account_id"])
                else:
                    deliverable(local_order, source_order_id, product["account_id"])
                if (local_order.get("item") or {}).get("id") != item["id"]:
                    raise RelistConflict("RELIST_CANDIDATE_INVALID")
            source = product["stock_item_id"]
            source_cost = self.store.setting(f"finance:{owner}:cost:{source}")
            target_cost = self.store.setting(f"finance:{owner}:cost:{item['id']}")
            if source_cost is not None and target_cost is None:
                self.store.set_setting(f"finance:{owner}:cost:{item['id']}", source_cost)
            if platform:
                old = db.execute(
                    "SELECT p.* FROM delivery_policies p JOIN logical_listings l ON l.item_id=p.item_id WHERE l.product_id=? AND json_extract(p.payload,'$.mode')='playerok' AND json_extract(p.payload,'$.enabled')=1 ORDER BY p.updated_at DESC LIMIT 1",
                    (product_id,),
                ).fetchone()
                if not old or self.store.setting(f"delivery:{old['item_id']}:enabled") is False:
                    raise Conflict("Автоматизация Playerok исходного товара остановлена")
                db.execute(
                    "INSERT INTO delivery_policies VALUES(?,?,?,?,?)",
                    (item["id"], old["revision"], owner, old["payload"], product["armed_at"]),
                )
            elif product["handler"] == "telegram_gifts":
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
                    (
                        item["id"],
                        *(
                            digest(item["description"]) if key == "description_sha256" else old[key]
                            for key in columns
                        ),
                    ),
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
                "UPDATE logical_listings SET title_snapshot=? WHERE item_id=? AND product_id=?",
                (marked_title(item["name"], product["code"]), item["id"], product_id),
            )
            db.execute(
                "UPDATE logical_products SET error_code=NULL WHERE id=? AND revision=?",
                (product_id, product["revision"]),
            )
        if not source_order_id and item["status"] == "APPROVED":
            event("SUCCESS", f"🔄 [{product['code']}] — лот перевыставлен, выдача сохранена")
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
        if order_id and self.store.rows(
            "SELECT 1 FROM orders WHERE id=? AND json_extract(payload,'$._created_at_conflict')=1",
            (order_id,),
        ):
            raise Conflict("Серверные даты создания сделки расходятся; требуется ручная проверка")
        binding = self.listing(item_id)
        if not binding:
            return None
        # Stock has its own admission-only gate: depletion must not revoke
        # reservations or prevent verified replenishment. Gifts obey local STOP.
        if (
            handler == "telegram_gifts"
            and self.store.setting("delivery:" + item_id + ":enabled", True) is False
        ):
            raise Conflict("Выдача товара выключена владельцем")
        if binding["state"] == "LEGACY" or len(binding["code"]) != 3:
            raise Conflict("Требуется подтверждённый код в названии товара")
        self.authority(binding["owner_id"], binding["account_id"])
        platform = json.loads(binding["config"]).get("platform_only") is True
        if platform and handler != "playerok":
            raise Conflict("Товар выдаёт Playerok; повторная передача ботом запрещена")
        if (
            binding["state"] != "VERIFIED"
            or not binding["enabled"]
            or not binding["verified_at"]
            or binding["revision"] != binding["config_revision"]
            or handler
            and not (platform and handler == "playerok")
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
                or not self.same_delivery_config(scope["config"], binding)
                for scope in scopes
            ):
                raise Conflict("Конфигурация заказа не совпадает с подтверждённой привязкой")
        return binding

    async def fresh_delivery_item(self, item_id, order_id=None):
        product = self.listing(item_id)
        if not product or product["state"] == "LEGACY":
            return
        if product["source_order_id"]:
            if order_id != product["source_order_id"]:
                raise Conflict("Снимок объявления разрешён только для исходной оплаченной сделки")
            from app.services.order_management import deal_details, deliverable
            from app.services.orders import Orders

            order = await deal_details(self.api, order_id, full_item=True, force_full=True)
            deliverable(order, order_id, product["account_id"])
            item = order.get("item") or {}
            Orders(self.store).ingest(order)
        else:
            item = await self.api.call("get_item", id=item_id)
        if (
            any(not isinstance(item.get(key), str) for key in ("id", "name", "status", "description"))
            or item.get("price") is None
            or any(
                not isinstance(item.get(key), dict) or not item[key].get("id")
                for key in ("user", "category", "obtaining_type")
            )
        ):
            raise APIError("DELIVERY_LISTING_DETAILS_NOT_YET_AVAILABLE")
        if (
            item.get("id") != item_id
            or item.get("deleted_at")
            or item.get("status") not in {"APPROVED", "SOLD"}
        ):
            raise Conflict("Состояние объявления не подтверждает выдачу оплаченного товара")
        await self.refresh_title(item)
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
            or row["status"]
            not in (
                {"PAID", "SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"}
                if handler == "playerok"
                else {"PAID"}
            )
            or row["item_id"] != item_id
            or json.loads(row["payload"]).get("_created_at_conflict")
            or not future_order(json.loads(row["payload"]).get("created_at"), product["armed_at"])
        ):
            raise Conflict("Заказ не подтверждён как новый оплаченный заказ этой конфигурации")
        transition = db.execute(
            "SELECT confirmed_at FROM logical_code_migrations WHERE product_id=?", (product["id"],)
        ).fetchone()
        if (
            transition
            and transition[0]
            and not future_order(json.loads(row["payload"]).get("created_at"), transition[0])
        ):
            raise Conflict("Заказ возник до подтверждения нового кода. Требуется ручная проверка")
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
        "UPDATE logical_listings SET verified_at=?,server_status=?,title_snapshot=? WHERE item_id=? AND operation_id=? AND config_revision=?",
        (
            now(),
            receipt["status"],
            receipt["title_snapshot"],
            receipt["item_id"],
            action["id"],
            receipt["revision"],
        ),
    )
    changed = db.execute(
        "UPDATE logical_products SET state='VERIFIED',baseline=?,updated_at=? WHERE id=? AND revision=? AND state='PENDING'",
        (encode(receipt["baseline"]), now(), receipt["product_id"], receipt["revision"]),
    ).rowcount
    if changed:
        from app.services.platform_orders import finish_platform_binding

        finish_platform_binding(db, action)
