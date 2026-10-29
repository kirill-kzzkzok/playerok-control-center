import hashlib
import json

from app.database.store import Conflict, encode, now


class Orders:
    def __init__(self, store):
        self.store = store

    def ingest(self, order):
        ident, status = order["id"], order["status"]
        item = order.get("item") or {}
        if not item.get("id") or not status or order.get("direction") != "OUT":
            raise ValueError("Неполная сделка продажи")
        key = f"order:{ident}:{status}"
        with self.store.transaction() as db:
            added = db.execute(
                "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at) VALUES(?,?,?,?)",
                (key, "order", encode(order), now()),
            ).rowcount
            previous = db.execute("SELECT status,payload FROM orders WHERE id=?", (ident,)).fetchone()
            terminal = {"CONFIRMED", "CONFIRMED_AUTOMATICALLY", "ROLLED_BACK", "FAILED"}
            stale = previous and (
                (previous["status"] in terminal and status not in terminal)
                or (previous["status"] == "SENT" and status in {"PAID", "PENDING"})
                or (previous["status"] == "PAID" and status == "PENDING")
            )
            if stale:
                db.execute("UPDATE inbox SET processed_at=? WHERE key=?", (now(), key))
                return False
            db.execute(
                "INSERT INTO orders(id,item_id,status,chat_id,amount,created_at,payload) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,chat_id=excluded.chat_id,amount=excluded.amount,payload=excluded.payload",
                (
                    ident,
                    item["id"],
                    status,
                    (order.get("chat") or {}).get("id"),
                    str(item["price"]) if item.get("price") is not None else None,
                    order.get("created_at") or now(),
                    encode(order),
                ),
            )
            db.execute("UPDATE inbox SET processed_at=? WHERE key=?", (now(), key))
            old_problem = json.loads(previous["payload"]).get("has_problem") if previous else None
            if not previous or previous["status"] != status or old_problem != order.get("has_problem"):
                history = db.execute(
                    "INSERT INTO order_history(order_id,source,old_status,new_status,problem,created_at) VALUES(?,'API_STATE',?,?,?,?)",
                    (
                        ident,
                        previous["status"] if previous else None,
                        status,
                        order.get("has_problem"),
                        now(),
                    ),
                )
                if previous and old_problem is not True and order.get("has_problem") is True:
                    db.execute(
                        "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) VALUES(?,'order_problem',?,?,?)",
                        (
                            f"order_problem:{ident}:{history.lastrowid}",
                            encode({"order_id": ident, "item_name": item.get("name") or item["id"]}),
                            now(),
                            now(),
                        ),
                    )
            return bool(added)

    def annotate(self, order_id, actor, revision, problem, note):
        if not isinstance(problem, bool) or not isinstance(note, str) or len(note.strip()) > 1000:
            raise ValueError("Комментарий до 1000 символов; признак проблемы должен быть задан явно")
        with self.store.transaction() as db:
            if not db.execute("SELECT 1 FROM administrators WHERE user_id=?", (actor,)).fetchone():
                raise Conflict("Доступ закрыт")
            order = db.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
            previous = db.execute(
                "SELECT revision FROM order_annotations WHERE order_id=?", (order_id,)
            ).fetchone()
            if not order or (previous[0] if previous else 0) != revision:
                raise Conflict("Отметка изменилась. Откройте заказ заново")
            db.execute(
                "INSERT INTO order_annotations VALUES(?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET problem=excluded.problem,note=excluded.note,revision=excluded.revision,actor=excluded.actor,updated_at=excluded.updated_at",
                (order_id, int(problem), note.strip(), revision + 1, actor, now()),
            )
            db.execute(
                "INSERT INTO order_history(order_id,source,new_status,problem,actor,note,created_at) VALUES(?,'LOCAL_NOTE',?,?,?,?,?)",
                (order_id, order[0], int(problem), actor, note.strip(), now()),
            )

    def add_stock(self, item_id, values):
        cleaned = [value.strip() for value in values if value.strip()]
        if not cleaned:
            raise ValueError("Пустой запас")
        with self.store.transaction() as db:
            total = 0
            for value in cleaned:
                fingerprint = hashlib.sha256(value.encode()).hexdigest()
                total += db.execute(
                    "INSERT OR IGNORE INTO inventory(item_id,secret,fingerprint) VALUES(?,?,?)",
                    (item_id, value, fingerprint),
                ).rowcount
            return total

    @staticmethod
    def empty_stock(db, item_id, order_id):
        db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"delivery:{item_id}:enabled", "false"),
        )
        last_id = db.execute(
            "SELECT COALESCE(MAX(id),0) FROM inventory WHERE item_id=?", (item_id,)
        ).fetchone()[0]
        db.execute(
            "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) VALUES(?,'stock_empty',?,?,?)",
            (
                f"stock_empty:{item_id}:{last_id}",
                encode({"item_id": item_id, "order_id": order_id}),
                now(),
                now(),
            ),
        )

    def reserve(self, order_id, actor=None):
        with self.store.transaction() as db:
            order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order or order["status"] != "PAID" or not order["chat_id"]:
                raise Conflict("Выдача разрешена только оплаченной продаже с известным чатом")
            existing = db.execute(
                "SELECT inventory_id FROM reservations WHERE order_id=?", (order_id,)
            ).fetchone()
            if existing:
                return existing[0]
            if db.execute("SELECT 1 FROM outbox WHERE operation_key=?", (f"delivery:{order_id}",)).fetchone():
                raise Conflict("Выдача этого заказа уже зарегистрирована. Проверьте историю операций")
            stock = db.execute(
                "SELECT id FROM inventory WHERE item_id=? AND status='AVAILABLE' ORDER BY id LIMIT 1",
                (order["item_id"],),
            ).fetchone()
            if not stock:
                self.empty_stock(db, order["item_id"], order_id)
                return None
            db.execute(
                "UPDATE inventory SET status='RESERVED' WHERE id=? AND status='AVAILABLE'", (stock[0],)
            )
            db.execute("INSERT INTO reservations VALUES(?,?)", (order_id, stock[0]))
            self.store.enqueue(f"delivery:{order_id}", "delivery", order_id, {"inventory_id": stock[0]}, db)
            db.execute(
                "INSERT INTO order_history(order_id,source,new_status,actor,note,created_at) VALUES(?,'DELIVERY_RESERVED',?,?,?,?)",
                (
                    order_id,
                    order["status"],
                    actor,
                    "Создан резерв уникальной единицы; отправка ещё не подтверждена",
                    now(),
                ),
            )
            if not db.execute(
                "SELECT 1 FROM inventory WHERE item_id=? AND status='AVAILABLE' LIMIT 1", (order["item_id"],)
            ).fetchone():
                self.empty_stock(db, order["item_id"], order_id)
            return stock[0]

    def ingest_message(self, chat_id, message):
        if not message.get("id"):
            raise ValueError("Сообщение без ID")
        key = f"message:{message['id']}"
        with self.store.transaction() as db:
            added = db.execute(
                "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at) VALUES(?,?,?,?)",
                (key, "message", encode(message), now()),
            ).rowcount
            db.execute(
                "INSERT INTO chat_messages VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (message["id"], chat_id, message.get("created_at"), encode(message)),
            )
            db.execute("UPDATE inbox SET processed_at=? WHERE key=?", (now(), key))
            return bool(added)
