import hashlib

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
            previous = db.execute("SELECT status FROM orders WHERE id=?", (ident,)).fetchone()
            terminal = {"CONFIRMED", "CONFIRMED_AUTOMATICALLY", "ROLLED_BACK"}
            if previous and previous[0] in terminal and status not in terminal:
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
            return bool(added)

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

    def reserve(self, order_id):
        with self.store.transaction() as db:
            order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order or order["status"] != "PAID" or not order["chat_id"]:
                raise Conflict("Выдача разрешена только оплаченной продаже с известным чатом")
            existing = db.execute(
                "SELECT inventory_id FROM reservations WHERE order_id=?", (order_id,)
            ).fetchone()
            if existing:
                return existing[0]
            stock = db.execute(
                "SELECT id FROM inventory WHERE item_id=? AND status='AVAILABLE' ORDER BY id LIMIT 1",
                (order["item_id"],),
            ).fetchone()
            if not stock:
                db.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"delivery:{order['item_id']}:enabled", "false"),
                )
                return None
            db.execute(
                "UPDATE inventory SET status='RESERVED' WHERE id=? AND status='AVAILABLE'", (stock[0],)
            )
            db.execute("INSERT INTO reservations VALUES(?,?)", (order_id, stock[0]))
            self.store.enqueue(f"delivery:{order_id}", "delivery", order_id, {"inventory_id": stock[0]}, db)
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
