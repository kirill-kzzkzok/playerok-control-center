import json
from dataclasses import asdict

from app.database.store import Conflict, encode, now
from app.services.automation import Automation, future_order
from app.services.order_management import deal_details, deliverable
from app.services.orders import Orders
from app.services.products import ValidationError


class DeliveryChains:
    """One durable sequence per order. Unknown steps block all later sends."""

    def __init__(self, store, api):
        self.store, self.api, self.policies = store, api, Automation(store, api)

    async def create(self, actor, ident, expected_revision=None, *, automatic=False):
        self.policies.orders.owner(actor)
        if not self.api.account.writes_enabled or not getattr(self.api.account, "automation_enabled", False):
            raise ValidationError("Исполнение цепочек выключено в локальной конфигурации")
        # Platform fulfilment does not transfer inventory/text a second time.
        candidate = self.store.rows("SELECT item_id FROM orders WHERE id=?", (ident,))
        revision, candidate_policy = self.policies.get(candidate[0]["item_id"] if candidate else "")
        if candidate_policy.mode == "playerok":
            from app.services.platform_orders import PlatformOrders

            if not automatic or expected_revision != revision:
                raise Conflict("Нужно действующее разрешение будущих покупок")
            return await PlatformOrders(self.store, self.api).create(actor, ident, revision)
        order = await self.policies.orders.prepare_delivery(actor, ident)
        revision, policy = self.policies.get(order["item"]["id"])
        if expected_revision is not None and revision != expected_revision:
            raise Conflict("Политика изменилась после подтверждения")
        if not policy.enabled or policy.mode not in {"text", "stock"}:
            raise ValidationError("Политика автоматики выключена или имеет ручной режим")
        item_id = order["item"]["id"]
        from app.services.logical_products import LogicalProducts, stock_scope

        await LogicalProducts(self.store, self.api).fresh_delivery_item(item_id, ident)
        with self.store.transaction() as db:
            LogicalProducts(self.store, self.api).pin_order(
                db, ident, item_id, "unique_stock" if policy.mode == "stock" else "fixed_text"
            )
            inventory_item = stock_scope(self.store, item_id, ident)
            if (
                db.execute("SELECT 1 FROM gift_jobs WHERE order_id=?", (ident,)).fetchone()
                or db.execute(
                    "SELECT 1 FROM gift_listings WHERE item_id=? AND enabled=1", (item_id,)
                ).fetchone()
            ):
                raise Conflict("Для этого заказа/лота уже настроена подарочная выдача")
            existing = db.execute(
                "SELECT order_id FROM delivery_chains WHERE order_id=?", (ident,)
            ).fetchone()
            if existing:
                return existing[0]
            current = db.execute(
                "SELECT revision,updated_at FROM delivery_policies WHERE item_id=?", (item_id,)
            ).fetchone()
            if not current or current[0] != revision:
                raise Conflict("Политика изменилась во время проверки")
            if automatic:
                guard = db.execute(
                    "SELECT value FROM settings WHERE key=?", (f"delivery:{item_id}:enabled",)
                ).fetchone()
                if (
                    not policy.auto_enroll
                    or not future_order(order.get("created_at"), current["updated_at"])
                    or (guard and json.loads(guard[0]) is False)
                ):
                    raise Conflict("Автоматическое включение заказа не разрешено")
            if db.execute(
                "SELECT 1 FROM order_annotations WHERE order_id=? AND problem=1", (ident,)
            ).fetchone():
                raise Conflict("Заказ имеет локальную проблему")
            local = db.execute("SELECT status FROM orders WHERE id=?", (ident,)).fetchone()
            if not local or local[0] != "PAID":
                raise Conflict("Сохранённая история не разрешает выдачу")
            if (
                db.execute("SELECT 1 FROM reservations WHERE order_id=?", (ident,)).fetchone()
                or db.execute(
                    "SELECT 1 FROM outbox WHERE target=? AND kind IN ('delivery','delivery_text','chain_message','fulfill')",
                    (ident,),
                ).fetchone()
            ):
                raise Conflict("Выдача или выполнение уже зарегистрированы")
            if policy.mode == "stock":
                stock = db.execute(
                    "SELECT id FROM inventory WHERE item_id=? AND status='AVAILABLE' ORDER BY id LIMIT 1",
                    (inventory_item,),
                ).fetchone()
                if not stock:
                    Orders.empty_stock(db, item_id, ident)
                    return None
                db.execute(
                    "UPDATE inventory SET status='RESERVED' WHERE id=? AND status='AVAILABLE'", (stock[0],)
                )
                db.execute("INSERT INTO reservations VALUES(?,?)", (ident, stock[0]))
            saved_policy = asdict(policy)
            binding = LogicalProducts(self.store, self.api).listing(item_id)
            if binding:
                saved_policy["_logical_authority"] = {
                    "product_id": binding["id"],
                    "control_revision": binding["control_revision"],
                }
            db.execute(
                "INSERT INTO delivery_chains VALUES(?,?,?,?,?,'ACTIVE',?)",
                (ident, item_id, actor, revision, encode(saved_policy), now()),
            )
            steps = []
            if policy.greeting:
                steps.append(("chain_message", policy.greeting))
            steps.append(
                (
                    "delivery" if policy.mode == "stock" else "delivery_text",
                    policy.text if policy.mode == "text" else "",
                )
            )
            if policy.after:
                steps.append(("chain_message", policy.after))
            db.executemany(
                "INSERT INTO delivery_chain_steps(order_id,position,kind,text) VALUES(?,?,?,?)",
                [(ident, index, kind, text) for index, (kind, text) in enumerate(steps)],
            )
            db.execute(
                "INSERT INTO order_history(order_id,source,new_status,actor,note,created_at) VALUES(?,'AUTOMATION_CHAIN','PAID',?,'Цепочка сохранена; отправка не подтверждена',?)",
                (ident, actor, now()),
            )
            if (
                policy.mode == "stock"
                and not db.execute(
                    "SELECT 1 FROM inventory WHERE item_id=? AND status='AVAILABLE' LIMIT 1",
                    (inventory_item,),
                ).fetchone()
            ):
                Orders.empty_stock(db, item_id, ident)
        return ident

    def policy_authorized(self, chain):
        """A stock admission resume does not revoke a frozen paid-order policy.

        Existing chains without a persisted control fence retain strict revision
        checks: an absent fence cannot prove that an owner STOP never happened.
        """
        revision, policy = self.policies.get(chain["item_id"])
        if not policy.enabled:
            return False
        saved = json.loads(chain["policy"])
        authority = saved.pop("_logical_authority", None)
        if authority:
            from app.services.logical_products import LogicalProducts

            binding = LogicalProducts(self.store, self.api).listing(chain["item_id"])
            if (
                not binding
                or authority
                != {
                    "product_id": binding["id"],
                    "control_revision": binding["control_revision"],
                }
                or not binding["enabled"]
            ):
                return False
        return revision == chain["policy_revision"] or bool(authority and asdict(policy) == saved)

    def freeze_stock_resume(self, db, item_id, revision, policy):
        """Upgrade an old snapshot before an admission-only revision change.

        Equal monotonic policy revisions plus an unchanged confirmed order scope
        prove there was no intervening STOP/re-arm. Never infer that proof for
        an already divergent old revision or an unbound legacy order.
        """
        from app.services.logical_products import LogicalProducts

        binding = LogicalProducts(self.store, self.api).listing(item_id)
        if (
            not binding
            or not binding["enabled"]
            or binding["state"] != "VERIFIED"
            or not binding["verified_at"]
            or binding["revision"] != binding["config_revision"]
        ):
            return
        for chain in db.execute(
            "SELECT c.* FROM delivery_chains c JOIN logical_order_scopes s ON s.order_id=c.order_id "
            "WHERE c.item_id=? AND c.actor=? AND c.policy_revision=? AND c.status='ACTIVE' "
            "AND s.product_id=? AND s.config_revision=? AND s.config=? AND s.item_id=c.item_id",
            (item_id, binding["owner_id"], revision, binding["id"], binding["revision"], binding["config"]),
        ).fetchall():
            saved = json.loads(chain["policy"])
            if saved != asdict(policy):
                continue
            saved["_logical_authority"] = {
                "product_id": binding["id"],
                "control_revision": binding["control_revision"],
            }
            db.execute(
                "UPDATE delivery_chains SET policy=? WHERE order_id=?", (encode(saved), chain["order_id"])
            )

    def advance(self, ident):
        with self.store.transaction() as db:
            chain = db.execute("SELECT * FROM delivery_chains WHERE order_id=?", (ident,)).fetchone()
            if not chain or chain["status"] != "ACTIVE":
                return False
            stop = not self.policy_authorized(chain)
            steps = db.execute(
                "SELECT s.*,o.status,o.started_at FROM delivery_chain_steps s LEFT JOIN outbox o ON o.id=s.operation_id WHERE s.order_id=? ORDER BY position",
                (ident,),
            ).fetchall()
            failed = any(step["status"] in {"UNCERTAIN", "PERMANENT_FAILED"} for step in steps)
            if stop or failed:
                db.execute(
                    "UPDATE delivery_chains SET status=? WHERE order_id=?",
                    ("CANCELLED" if stop else "BLOCKED", ident),
                )
                if any(
                    step["kind"] == "delivery"
                    and (step["started_at"] is not None or step["status"] == "UNCERTAIN")
                    for step in steps
                ):
                    db.execute(
                        "UPDATE inventory SET status='QUARANTINED' WHERE id=(SELECT inventory_id FROM reservations WHERE order_id=?) AND status='RESERVED'",
                        (ident,),
                    )
                return True
            for step in steps:
                if step["operation_id"]:
                    if step["status"] != "SUCCEEDED":
                        return False
                    continue
                payload = {"chain_id": ident, "chain_position": step["position"], "text": step["text"]}
                if json.loads(chain["policy"]).get("mode") == "playerok":
                    payload.update(platform_followup=True, mode="legacy", report={})
                if step["kind"] == "delivery":
                    payload["inventory_id"] = db.execute(
                        "SELECT inventory_id FROM reservations WHERE order_id=?", (ident,)
                    ).fetchone()[0]
                    key = "delivery:" + ident
                else:
                    key = f"chain:{ident}:{step['position']}"
                operation = self.store.enqueue(key, step["kind"], ident, payload, db=db)
                db.execute(
                    "UPDATE delivery_chain_steps SET operation_id=? WHERE order_id=? AND position=?",
                    (operation, ident, step["position"]),
                )
                return True
            saved = json.loads(chain["policy"])
            status = (
                "NEEDS_FULFILLMENT"
                if saved.get("mark_sent") and saved.get("mode") != "playerok"
                else "SUCCEEDED"
            )
            db.execute("UPDATE delivery_chains SET status=? WHERE order_id=?", (status, ident))
            return True

    def dispatchable(self, action):
        if not getattr(self.api.account, "automation_enabled", False):
            raise ValidationError("Исполнение цепочек выключено")
        ident, payload = action["target"], action["payload"]
        rows = self.store.rows(
            "SELECT c.*,s.position,s.text,s.kind,s.operation_id FROM delivery_chains c JOIN delivery_chain_steps s ON s.order_id=c.order_id WHERE c.order_id=? AND s.position=?",
            (ident, payload.get("chain_position")),
        )
        if not rows:
            raise ValidationError("Шаг цепочки не зарегистрирован")
        step = rows[0]
        from app.services.logical_products import LogicalProducts

        platform = json.loads(step["policy"]).get("mode") == "playerok"
        if platform:
            from app.services.platform_orders import PlatformOrders

            PlatformOrders(self.store, self.api).guard(action)
            if payload.get("platform_followup") is not True:
                raise ValidationError("Не подтверждён режим выдачи Playerok")
        else:
            LogicalProducts(self.store, self.api).require_delivery(step["item_id"], order_id=ident)
        if (
            step["status"] != "ACTIVE"
            or step["operation_id"] != action["id"]
            or step["kind"] != action["kind"]
            or payload.get("chain_id") != ident
            or payload.get("text") != step["text"]
        ):
            raise ValidationError("Шаг цепочки изменился или остановлен")
        if not self.policy_authorized(step):
            raise ValidationError("Политика выключена или изменилась")
        earlier = self.store.rows(
            "SELECT o.status FROM delivery_chain_steps s LEFT JOIN outbox o ON o.id=s.operation_id WHERE s.order_id=? AND s.position<?",
            (ident, step["position"]),
        )
        if any(row["status"] != "SUCCEEDED" for row in earlier):
            raise ValidationError("Предыдущий шаг ещё не подтверждён")
        if self.store.rows("SELECT 1 FROM order_annotations WHERE order_id=? AND problem=1", (ident,)):
            raise ValidationError("Локальная проблема блокирует автоматическую отправку")
        current = self.store.rows("SELECT status,payload FROM orders WHERE id=?", (ident,))
        if not current or (not platform and current[0]["status"] != "PAID"):
            raise ValidationError("Сохранённая история запрещает автоматическую отправку")
        if not platform:
            deliverable(json.loads(current[0]["payload"]), ident, self.api.account.id)
        return step

    async def verify_step(self, action):
        step = self.dispatchable(action)
        if json.loads(step["policy"]).get("mode") == "playerok":
            from app.services.platform_orders import PlatformOrders

            order = await PlatformOrders(self.store, self.api).verify(action)
            if action["kind"] == "fulfill":
                return None
            chat_id = (order.get("chat") or {}).get("id")
            if not chat_id:
                from app.playerok.adapter import APIError

                raise APIError("PLATFORM_CHAT_UNAVAILABLE")
            chat = await self.api.call("get_chat", chat_id=chat_id)
            if chat.get("id") != chat_id or chat.get("is_texting_allowed") is not True:
                from app.playerok.adapter import APIError

                raise APIError("PLATFORM_CHAT_UNAVAILABLE")
            self.dispatchable(action)
            return chat_id
        from app.services.logical_products import LogicalProducts

        await LogicalProducts(self.store, self.api).fresh_delivery_item(step["item_id"], action["target"])
        ident = action["target"]
        order = await deal_details(self.api, ident)
        deliverable(order, ident, self.api.account.id)
        if order["item"]["id"] != step["item_id"]:
            raise ValidationError("Предмет сделки изменился")
        chat = await self.api.call("get_chat", chat_id=order["chat"]["id"])
        if chat.get("id") != order["chat"]["id"] or chat.get("is_texting_allowed") is not True:
            raise ValidationError("Чат закрыт для отправки")
        if self.store.rows("SELECT 1 FROM order_annotations WHERE order_id=? AND problem=1", (ident,)):
            raise ValidationError("Локальная проблема блокирует автоматическую отправку")
        if not self.policy_authorized(step) or not self.store.rows(
            "SELECT 1 FROM delivery_chains WHERE order_id=? AND status='ACTIVE'", (ident,)
        ):
            raise ValidationError("Цепочка остановлена во время проверки")
        return order["chat"]["id"]
