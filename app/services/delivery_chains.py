import json
from dataclasses import asdict

from app.database.store import Conflict, encode, now
from app.services.automation import Automation
from app.services.order_management import deliverable
from app.services.orders import Orders
from app.services.products import ValidationError


class DeliveryChains:
    """One durable sequence per order. Unknown steps block all later sends."""

    def __init__(self, store, api):
        self.store, self.api, self.policies = store, api, Automation(store, api)

    async def create(self, actor, ident, expected_revision=None):
        self.policies.orders.owner(actor)
        if not self.api.account.writes_enabled or not getattr(self.api.account, "automation_enabled", False):
            raise ValidationError("Исполнение цепочек выключено в локальной конфигурации")
        order = await self.policies.orders.prepare_delivery(actor, ident)
        revision, policy = self.policies.get(order["item"]["id"])
        if expected_revision is not None and revision != expected_revision:
            raise Conflict("Политика изменилась после подтверждения")
        if not policy.enabled or policy.mode not in {"text", "stock"}:
            raise ValidationError("Политика автоматики выключена или имеет ручной режим")
        item_id = order["item"]["id"]
        with self.store.transaction() as db:
            existing = db.execute(
                "SELECT order_id FROM delivery_chains WHERE order_id=?", (ident,)
            ).fetchone()
            if existing:
                return existing[0]
            current = db.execute(
                "SELECT revision FROM delivery_policies WHERE item_id=?", (item_id,)
            ).fetchone()
            if not current or current[0] != revision:
                raise Conflict("Политика изменилась во время проверки")
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
                    (item_id,),
                ).fetchone()
                if not stock:
                    Orders.empty_stock(db, item_id, ident)
                    return None
                db.execute(
                    "UPDATE inventory SET status='RESERVED' WHERE id=? AND status='AVAILABLE'", (stock[0],)
                )
                db.execute("INSERT INTO reservations VALUES(?,?)", (ident, stock[0]))
            db.execute(
                "INSERT INTO delivery_chains VALUES(?,?,?,?,?,'ACTIVE',?)",
                (ident, item_id, actor, revision, encode(asdict(policy)), now()),
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
                    "SELECT 1 FROM inventory WHERE item_id=? AND status='AVAILABLE' LIMIT 1", (item_id,)
                ).fetchone()
            ):
                Orders.empty_stock(db, item_id, ident)
        return ident

    def advance(self, ident):
        with self.store.transaction() as db:
            chain = db.execute("SELECT * FROM delivery_chains WHERE order_id=?", (ident,)).fetchone()
            if not chain or chain["status"] != "ACTIVE":
                return False
            policy = db.execute(
                "SELECT revision,payload FROM delivery_policies WHERE item_id=?", (chain["item_id"],)
            ).fetchone()
            stop = (
                not policy
                or policy["revision"] != chain["policy_revision"]
                or not json.loads(policy["payload"]).get("enabled")
            )
            steps = db.execute(
                "SELECT s.*,o.status FROM delivery_chain_steps s LEFT JOIN outbox o ON o.id=s.operation_id WHERE s.order_id=? ORDER BY position",
                (ident,),
            ).fetchall()
            failed = any(step["status"] in {"UNCERTAIN", "PERMANENT_FAILED"} for step in steps)
            if stop or failed:
                db.execute(
                    "UPDATE delivery_chains SET status=? WHERE order_id=?",
                    ("CANCELLED" if stop else "BLOCKED", ident),
                )
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
            status = "NEEDS_FULFILLMENT" if json.loads(chain["policy"]).get("mark_sent") else "SUCCEEDED"
            db.execute("UPDATE delivery_chains SET status=? WHERE order_id=?", (status, ident))
            return True

    async def verify_step(self, action):
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
        if (
            step["status"] != "ACTIVE"
            or step["operation_id"] != action["id"]
            or step["kind"] != action["kind"]
            or payload.get("chain_id") != ident
            or payload.get("text") != step["text"]
        ):
            raise ValidationError("Шаг цепочки изменился или остановлен")
        revision, policy = self.policies.get(step["item_id"])
        if revision != step["policy_revision"] or not policy.enabled:
            raise ValidationError("Политика выключена или изменилась")
        earlier = self.store.rows(
            "SELECT o.status FROM delivery_chain_steps s LEFT JOIN outbox o ON o.id=s.operation_id WHERE s.order_id=? AND s.position<?",
            (ident, step["position"]),
        )
        if any(row["status"] != "SUCCEEDED" for row in earlier):
            raise ValidationError("Предыдущий шаг ещё не подтверждён")
        order = await self.api.call("get_deal", deal_id=ident)
        deliverable(order, ident, self.api.account.id)
        if order["item"]["id"] != step["item_id"]:
            raise ValidationError("Предмет сделки изменился")
        chat = await self.api.call("get_chat", chat_id=order["chat"]["id"])
        if chat.get("id") != order["chat"]["id"] or chat.get("is_texting_allowed") is not True:
            raise ValidationError("Чат закрыт для отправки")
        if self.store.rows("SELECT 1 FROM order_annotations WHERE order_id=? AND problem=1", (ident,)):
            raise ValidationError("Локальная проблема блокирует автоматическую отправку")
        latest_revision, latest_policy = self.policies.get(step["item_id"])
        if latest_revision != step["policy_revision"] or not latest_policy.enabled or not self.store.rows("SELECT 1 FROM delivery_chains WHERE order_id=? AND status='ACTIVE'", (ident,)):
            raise ValidationError("Цепочка остановлена во время проверки")
        return order["chat"]["id"]
