import hashlib
import json

from app.database.store import Conflict, encode, now, server_time
from app.security.logging import event


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
            old_payload = json.loads(previous["payload"]) if previous else {}
            old_item = old_payload.get("item") or {}
            previous_created = server_time(old_payload.get("created_at"))
            observed_created = server_time(order.get("created_at"))
            if previous_created and observed_created and previous_created != observed_created:
                # Keep the most conservative upstream evidence. Short snapshots
                # can truncate dates; later corrections must never lift an old
                # sale across the owner's cutoff or replace it with receive time.
                order = {**order, "created_at": min(previous_created, observed_created)}
            for before, after in (
                (old_item.get("id"), item.get("id")),
                ((old_item.get("user") or {}).get("id"), (item.get("user") or {}).get("id")),
                ((old_payload.get("user") or {}).get("id"), (order.get("user") or {}).get("id")),
                ((old_payload.get("chat") or {}).get("id"), (order.get("chat") or {}).get("id")),
            ):
                if before and after and before != after:
                    raise Conflict("Идентичность сохранённой сделки расходится с серверным ответом")
            # Retain observations for history/UI, never as fresh dispatch proof.
            # Explicit empty values are real observations; absent/null omissions
            # remain distinguishable in provenance. No status/problem flags merge.
            order, item = dict(order), dict(item)
            retained = {}
            for target, source, fields, prefix in (
                (order, old_payload, ("obtaining_fields", "user"), ""),
                (
                    item,
                    old_item,
                    (
                        "name",
                        "description",
                        "category",
                        "obtaining_type",
                        "attributes",
                        "data_fields",
                        "comment",
                        "user",
                    ),
                    "item.",
                ),
            ):
                for field in fields:
                    if target.get(field) is None and source.get(field) is not None:
                        retained[prefix + field] = "null" if field in target else "absent"
                        target[field] = source[field]
            order["item"] = item
            if retained:
                order["_retained_fields"] = retained
            else:
                order.pop("_retained_fields", None)
            # Chat identity is immutable for the same order/item. A partial list
            # response omitting it must not erase a previously observed detail.
            # Real chat availability is still checked freshly before sending.
            if (
                not (order.get("chat") or {}).get("id")
                and (old_payload.get("chat") or {}).get("id")
                and (old_payload.get("item") or {}).get("id") == item["id"]
            ):
                order = {**order, "chat": old_payload["chat"]}
            # List responses can omit the immutable upstream date supplied by get_deal.
            # Preserve that date in the payload; the SQL ingest fallback is not evidence.
            if (
                server_time(order.get("created_at")) is None
                and server_time(old_payload.get("created_at")) is not None
            ):
                order = {**order, "created_at": old_payload["created_at"]}
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
            # Missing upstream dates are not permanent refusals. A later API
            # observation may prove eligibility against the ORIGINAL policy
            # cutoff; no owner stop, blocked check or existing chain is reset.
            from app.services.automation import future_order

            policy = db.execute(
                "SELECT revision,updated_at FROM delivery_policies WHERE item_id=?",
                (item["id"],),
            ).fetchone()
            if policy and future_order(order.get("created_at"), policy["updated_at"]):
                db.execute(
                    "UPDATE auto_enrollment SET status='PENDING',error_code=NULL "
                    "WHERE order_id=? AND policy_revision=? AND status='SKIPPED' "
                    "AND error_code='NOT_A_VERIFIED_FUTURE_ORDER' "
                    "AND NOT EXISTS(SELECT 1 FROM delivery_chains WHERE order_id=?)",
                    (ident, policy["revision"], ident),
                )
            old_problem = old_payload.get("has_problem")
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
            if added and status == "PAID":
                gift = db.execute(
                    "SELECT enabled FROM gift_listings WHERE item_id=?", (item["id"],)
                ).fetchone()
                policy = db.execute(
                    "SELECT payload FROM delivery_policies WHERE item_id=?", (item["id"],)
                ).fetchone()
                mode = (
                    ("Telegram-подарки" if gift[0] else "Telegram-подарки — выдача выключена")
                    if gift
                    else (
                        {
                            "manual": "Ручная выдача",
                            "stock": "Уникальные данные со склада",
                            "text": "Постоянный текст",
                            "playerok": "Выдаёт Playerok (бот только отвечает и отмечает выполнение)",
                        }.get(json.loads(policy[0]).get("mode"), "Способ выдачи не подтверждён")
                        if policy
                        else "Привязка не найдена — ожидается сверка"
                    )
                )
                event(
                    "ORDER",
                    "Обнаружен оплаченный заказ · товар: " + str(item.get("name") or item["id"])[:120],
                    order=ident,
                )
                event("ORDER", "Режим выдачи: " + mode, order=ident)
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
        from app.services.logical_products import stock_scope

        cleaned = [value.strip() for value in values if value.strip()]
        if not cleaned:
            raise ValueError("Пустой запас")
        with self.store.transaction() as db:
            scope = stock_scope(self.store, item_id)
            total = 0
            for value in cleaned:
                fingerprint = hashlib.sha256(value.encode()).hexdigest()
                total += db.execute(
                    "INSERT OR IGNORE INTO inventory(item_id,secret,fingerprint) VALUES(?,?,?)",
                    (scope, value, fingerprint),
                ).rowcount
            return total

    @staticmethod
    def empty_stock(db, item_id, order_id):
        product = db.execute(
            "SELECT p.id,p.stock_item_id FROM logical_products p JOIN logical_listings l ON l.product_id=p.id "
            "WHERE l.item_id=? AND p.handler='unique_stock' AND p.state='VERIFIED' AND l.verified_at IS NOT NULL",
            (item_id,),
        ).fetchone()
        aliases = [item_id]
        scope = item_id
        if product:
            scope = product["stock_item_id"]
            aliases = [
                row[0]
                for row in db.execute(
                    "SELECT item_id FROM logical_listings WHERE product_id=?", (product["id"],)
                )
            ]
        for alias in aliases:
            db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"delivery:{alias}:enabled", "false"),
            )
        last_id = db.execute(
            "SELECT COALESCE(MAX(id),0) FROM inventory WHERE item_id=?", (scope,)
        ).fetchone()[0]
        db.execute(
            "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) VALUES(?,'stock_empty',?,?,?)",
            (
                f"stock_empty:{scope}:{last_id}",
                encode({"item_id": item_id, "order_id": order_id}),
                now(),
                now(),
            ),
        )

    def reserve(self, order_id, actor=None):
        from app.services.logical_products import LogicalProducts, stock_scope

        with self.store.transaction() as db:
            order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if order and (
                db.execute("SELECT 1 FROM gift_jobs WHERE order_id=?", (order_id,)).fetchone()
                or db.execute(
                    "SELECT 1 FROM gift_listings WHERE item_id=? AND enabled=1", (order["item_id"],)
                ).fetchone()
            ):
                raise Conflict("Для заказа настроена подарочная выдача; складское резервирование запрещено")
            if not order or order["status"] != "PAID" or not order["chat_id"]:
                raise Conflict("Выдача разрешена только оплаченной продаже с известным чатом")
            if db.execute(
                "SELECT 1 FROM order_annotations WHERE order_id=? AND problem=1", (order_id,)
            ).fetchone():
                raise Conflict("Заказ требует решения продавца; резерв не создан")
            payload = json.loads(order["payload"])
            if (
                payload.get("has_problem") is not False
                or payload.get("is_automated") is not False
                or payload.get("direction") != "OUT"
            ):
                raise Conflict("Нужна подтверждённая продажа без проблемы и серверной выдачи")
            seller = ((payload.get("item") or {}).get("user") or {}).get("id")
            account = self.store.setting("playerok:account_id")
            if account and seller != account:
                raise Conflict("Продавец заказа не соответствует привязке аккаунта")
            policy = db.execute(
                "SELECT payload FROM delivery_policies WHERE item_id=?", (order["item_id"],)
            ).fetchone()
            if policy and json.loads(policy[0]).get("mode") != "stock":
                raise Conflict("Этот способ выдачи не использует уникальный склад")
            LogicalProducts(self.store).pin_order(db, order_id, order["item_id"], "unique_stock")
            scope = stock_scope(self.store, order["item_id"], order_id)
            existing = db.execute(
                "SELECT inventory_id FROM reservations WHERE order_id=?", (order_id,)
            ).fetchone()
            if existing:
                return existing[0]
            if db.execute("SELECT 1 FROM outbox WHERE operation_key=?", (f"delivery:{order_id}",)).fetchone():
                raise Conflict("Выдача этого заказа уже зарегистрирована. Проверьте историю операций")
            stock = db.execute(
                "SELECT id FROM inventory WHERE item_id=? AND status='AVAILABLE' ORDER BY id LIMIT 1",
                (scope,),
            ).fetchone()
            if not stock:
                self.empty_stock(db, order["item_id"], order_id)
                return None
            db.execute(
                "UPDATE inventory SET status='RESERVED' WHERE id=? AND status='AVAILABLE'", (stock[0],)
            )
            db.execute("INSERT INTO reservations VALUES(?,?)", (order_id, stock[0]))
            self.store.enqueue(
                f"delivery:{order_id}", "delivery", order_id, {"inventory_id": stock[0]}, db, actor=actor
            )
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
                "SELECT 1 FROM inventory WHERE item_id=? AND status='AVAILABLE' LIMIT 1", (scope,)
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
            previous = db.execute("SELECT payload FROM chat_messages WHERE id=?", (message["id"],)).fetchone()
            if previous and previous[0] != encode(message):
                db.execute(
                    "INSERT INTO chat_message_versions(message_id,payload,observed_at) VALUES(?,?,?)",
                    (message["id"], previous[0], now()),
                )
            if previous and previous[0] != encode(message):
                db.execute("UPDATE notifications SET rendered_payload=NULL WHERE event_key=?", (key,))
            db.execute("UPDATE inbox SET payload=? WHERE key=?", (encode(message), key))
            db.execute(
                "INSERT INTO chat_messages VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,created_at=COALESCE(excluded.created_at,chat_messages.created_at)",
                (message["id"], chat_id, message.get("created_at"), encode(message)),
            )
            db.execute("UPDATE inbox SET processed_at=? WHERE key=?", (now(), key))
            return bool(added)
