"""Prepare existing delivery services on a server DRAFT before queuing publication."""

import hashlib
from dataclasses import asdict

from app.database.store import Conflict, encode, now
from app.services.automation import Automation, DeliveryPolicy, message_text
from app.services.finance import money
from app.services.inventory import Inventory
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
        fresh = await self.check(owner, value)
        if digest(fresh) != digest(value["ready"]):
            raise Conflict("Товар, тариф или подарок изменились. Подтвердите новое превью")
        with self.store.transaction() as db:
            if (
                db.execute(
                    "UPDATE drafts SET state='QUEUED' WHERE id=? AND revision=? AND state='DRAFT'",
                    (ident, revision),
                ).rowcount
                != 1
            ):
                raise Conflict("Запуск уже подтверждён или настройки изменились")
            self.store.set_setting(
                "launch:prepared:" + value["launch_target"],
                {"id": ident, "owner": owner, "revision": revision, "operation": None, "at": now()},
            )
        item = value["launch_target"]
        paid_before = None
        # Tracks the EXACT row revisions this launch wrote (delivery policy / gift
        # listing). The rollback restores only rows still at a launch-written
        # revision: anything newer belongs to a concurrent owner action and is
        # never overwritten (lost-update protection).
        launch_written = {"delivery_policy": None, "gift_listing": None}
        # Snapshot of the delivery state BEFORE this launch modifies it. The rollback
        # restores exactly this: a pre-existing owner-configured policy must survive
        # a failed launch that never created it.
        delivery_before = {
            "policy": self.store.rows("SELECT * FROM delivery_policies WHERE item_id=?", (item,))[0]
            if self.store.rows("SELECT 1 FROM delivery_policies WHERE item_id=?", (item,))
            else None,
            "enabled_setting": self.store.setting(f"delivery:{item}:enabled"),
            "gift_listing": self.store.rows("SELECT * FROM gift_listings WHERE item_id=?", (item,))[0]
            if self.store.rows("SELECT 1 FROM gift_listings WHERE item_id=?", (item,))
            else None,
            "listing_options": self.store.setting("gifts:listing_options:" + item),
        }
        try:
            if value["publication"] == "PREMIUM" and value["promotion"]:
                # Only the schedule enable flag may be written, and only against the
                # exact revision confirmed in the readiness digest: other lots' limits,
                # schedule and selections are never touched by this launch.
                settings = PromotionSettings(self.store, self.api)
                p_revision, existing, selection = settings.get()
                if asdict(existing) != {**value["promotion_policy"], "enabled": existing.enabled}:
                    raise Conflict("Общие настройки продвижения изменились. Подтвердите новые лимиты")
                if not existing.enabled:
                    settings.save(
                        owner, p_revision, {**value["promotion_policy"], "enabled": True}, selection
                    )
            if value["mode"] == "gifts":
                s = self.gifts
                await s.bind(
                    owner,
                    item,
                    ", ".join(g["code"] for g in fresh["bundle"]),
                    expected_bundle=fresh["bundle"],
                )
                await s.arm(
                    owner,
                    item,
                    s.listing(item)["revision"],
                    auto_fulfill=value["auto_complete"],
                    prepublication=True,
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
                delivery_revision = s.listing(item)["revision"]
                launch_written["gift_listing"] = delivery_revision
            elif value["mode"] in {"text", "stock"}:
                if value["mode"] == "stock":
                    inventory = Inventory(self.store, self.api)
                    await inventory.import_values(
                        owner, item, value["data"], inventory.preview(item, value["data"]), "DRAFT"
                    )
                version, _ = self.automation.get(item)
                policy = DeliveryPolicy(
                    True,
                    value["mode"],
                    value["data"] if value["mode"] == "text" else "",
                    value["greeting"],
                    value["after"],
                    value["auto_complete"],
                    True,
                )
                delivery_revision = await self.automation.save(
                    owner, item, version, asdict(policy), future_confirmed=True
                )
                launch_written["delivery_policy"] = delivery_revision
            else:
                delivery_revision = 0
                if (
                    self.gifts.listing(item)
                    and self.gifts.listing(item)["enabled"]
                    or self.automation.get(item)[1].enabled
                ):
                    raise Conflict("Для ручного режима сначала остановите существующую выдачу товара")
            if value["publication"] == "PREMIUM":
                paid = PaidPromotions(self.store, self.api)
                preview = await paid.preview(owner, item, fresh["quote"]["id"])
                if money(preview["amount"]) != money(fresh["quote"]["price"]):
                    raise Conflict("Цена PREMIUM изменилась при подготовке. Подтвердите новое превью")
                if value["promotion"]:
                    # Provenance snapshot: what existed BEFORE this launch touched the
                    # paid-permission state. The rollback removes only what we created.
                    pre_existing = {
                        "approval": self.store.setting("promotions:approved:" + item) or None,
                        "authority": self.store.setting("promotions:paid_authority") or None,
                        "target": self.store.rows(
                            "SELECT disposition FROM promotion_targets WHERE item_id=?", (item,)
                        )[0]["disposition"]
                        if self.store.rows("SELECT 1 FROM promotion_targets WHERE item_id=?", (item,))
                        else None,
                    }
                    await paid.approve(owner, preview, prepublication=True)
                    await paid.settings.target(owner, item, "include")
                    paid.arm(owner, preview["policy_revision"])
                    paid_before = pre_existing
                launch = {
                    "id": ident,
                    "owner": owner,
                    "revision": revision,
                    "delivery_revision": delivery_revision,
                    "at": now(),
                }
                operation = await paid.queue(owner, preview, launch_key(ident, revision), launch=launch)
            else:
                with self.store.transaction() as db:
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
        except BaseException:
            # Never re-open a draft after a publication operation may already exist.
            # Match ONLY this revision's operation: older revisions' dead operations
            # must not keep the new attempt stuck in QUEUED forever. A revision is
            # never queued twice (draft CAS), so an existing key means THIS attempt
            # enqueued it and owns the outcome.
            with self.store.transaction() as db:
                if not db.execute(
                    "SELECT 1 FROM outbox WHERE operation_key=?", (launch_key(ident, revision),)
                ).fetchone():
                    # Preparation never enqueued the publication: restore the draft and
                    # drop the marker; delivery/paid state is restored from the
                    # pre-launch snapshots below (NOT blind release_launch disabling,
                    # which would also kill pre-existing owner configuration).
                    db.execute(
                        "UPDATE drafts SET state='DRAFT',revision=revision+1 WHERE id=? AND state='QUEUED'",
                        (ident,),
                    )
                    db.execute("DELETE FROM settings WHERE key=?", ("launch:prepared:" + item,))
                    # Restore the delivery state EXACTLY as it was before this launch:
                    # a failed launch must not disable a pre-existing owner policy.
                    # CAS by launch-written revision: if the current revision is NEWER
                    # than what this launch wrote, a concurrent owner action owns the
                    # row and is never overwritten.
                    before = delivery_before
                    current_policy = db.execute(
                        "SELECT revision FROM delivery_policies WHERE item_id=?", (item,)
                    ).fetchone()
                    launch_policy_rev = launch_written["delivery_policy"]
                    if (
                        launch_policy_rev is not None
                        and current_policy
                        and current_policy["revision"] == launch_policy_rev
                    ):
                        if before["policy"] is None:
                            db.execute("DELETE FROM delivery_policies WHERE item_id=?", (item,))
                        else:
                            snap = before["policy"]
                            db.execute(
                                "UPDATE delivery_policies SET revision=revision+1,actor=?,payload=?,updated_at=? "
                                "WHERE item_id=?",
                                (owner, snap["payload"], snap["updated_at"], item),
                            )
                    if self.store.setting(f"delivery:{item}:enabled") != before["enabled_setting"]:
                        if before["enabled_setting"] is None:
                            db.execute("DELETE FROM settings WHERE key=?", (f"delivery:{item}:enabled",))
                        else:
                            db.execute(
                                "INSERT INTO settings(key,value) VALUES(?,?) "
                                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (f"delivery:{item}:enabled", encode(before["enabled_setting"])),
                            )
                    current_listing = db.execute(
                        "SELECT revision,enabled FROM gift_listings WHERE item_id=?", (item,)
                    ).fetchone()
                    launch_listing_rev = launch_written["gift_listing"]
                    if (
                        launch_listing_rev is not None
                        and current_listing
                        and current_listing["revision"] == launch_listing_rev
                    ):
                        if before["gift_listing"] is None:
                            db.execute("DELETE FROM gift_listings WHERE item_id=?", (item,))
                        else:
                            snap = before["gift_listing"]
                            db.execute(
                                "UPDATE gift_listings SET revision=revision+1,gift_code=?,gift_id=?,"
                                "approved_stars=?,description_sha256=?,category_id=?,obtaining_type_id=?,"
                                "username_field_id=?,bundle=?,enabled=?,auto_fulfill=?,armed_at=? "
                                "WHERE item_id=?",
                                (
                                    snap["gift_code"],
                                    snap["gift_id"],
                                    snap["approved_stars"],
                                    snap["description_sha256"],
                                    snap["category_id"],
                                    snap["obtaining_type_id"],
                                    snap["username_field_id"],
                                    snap["bundle"],
                                    snap["enabled"],
                                    snap["auto_fulfill"],
                                    snap["armed_at"],
                                    item,
                                ),
                            )
                    if before["listing_options"] is not None:
                        current_options = self.store.setting("gifts:listing_options:" + item)
                        if current_options != before["listing_options"]:
                            db.execute(
                                "INSERT INTO settings(key,value) VALUES(?,?) "
                                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                ("gifts:listing_options:" + item, encode(before["listing_options"])),
                            )
                    if paid_before is not None:
                        # Undo ONLY the paid permissions this launch created; anything
                        # the owner had before stays untouched (other lots keep working).
                        before = paid_before
                        now_approval = db.execute(
                            "SELECT value FROM settings WHERE key=?", ("promotions:approved:" + item,)
                        ).fetchone()
                        if before["approval"] is None and now_approval is not None:
                            db.execute("DELETE FROM settings WHERE key=?", ("promotions:approved:" + item,))
                        elif before["approval"] is not None:
                            db.execute(
                                "UPDATE settings SET value=? WHERE key=?",
                                (encode(before["approval"]), "promotions:approved:" + item),
                            )
                        current_authority = self.store.setting("promotions:paid_authority") or None
                        if before["authority"] is None and current_authority is not None:
                            db.execute("DELETE FROM settings WHERE key='promotions:paid_authority'")
                        elif before["authority"] is not None:
                            db.execute(
                                "UPDATE settings SET value=? WHERE key='promotions:paid_authority'",
                                (encode(before["authority"]),),
                            )
                        if before["target"] is None:
                            db.execute("DELETE FROM promotion_targets WHERE item_id=?", (item,))
                        else:
                            db.execute(
                                "UPDATE promotion_targets SET disposition=? WHERE item_id=?",
                                (before["target"], item),
                            )
            raise

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
