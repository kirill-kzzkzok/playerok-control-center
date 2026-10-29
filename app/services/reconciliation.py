import hashlib
import hmac
import json
from datetime import datetime

from app.database.store import Conflict, encode, now
from app.playerok.adapter import validate_page


def signature(action):
    return hashlib.sha256(
        encode(
            {key: action[key] for key in ("id", "kind", "target", "payload", "created_at", "status")}
        ).encode()
    ).hexdigest()


class Reconciliation:
    """Positive read evidence + explicit owner decision; never requeue unknown writes."""

    def __init__(self, store, api):
        self.store, self.api = store, api

    def owner(self, actor):
        if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (actor,)):
            raise Conflict("Доступ закрыт")

    def action(self, actor, ident, *, uncertain=False):
        self.owner(actor)
        rows = self.store.rows("SELECT * FROM outbox WHERE id=?", (ident,))
        if not rows or (uncertain and rows[0]["status"] != "UNCERTAIN"):
            raise Conflict("Операция недоступна или уже разрешена")
        return rows[0]

    async def evidence(self, actor, ident, message_id=None):
        action = self.action(actor, ident, uncertain=True)
        kind, target = action["kind"], action["target"]
        payload = json.loads(action["payload"])
        if kind == "fulfill":
            from app.services.fulfillment import Fulfillment

            confirmed = await Fulfillment(self.store, self.api).result(action)
            if not confirmed:
                raise Conflict("Выполнение и требуемый отчёт не подтверждены; повтор запрещён")
            return {"decision": "CONFIRM_READ_STATE", **confirmed, "checked_at": now()}
        if kind == "mark_read":
            chat = await self.api.call("get_chat", chat_id=target)
            if chat.get("id") != target or chat.get("unread_messages_counter") != 0:
                raise Conflict("Чат не подтверждён как прочитанный")
            return {
                "decision": "CONFIRM_READ_STATE",
                "chat_id": target,
                "unread_messages_counter": 0,
                "checked_at": now(),
            }
        if kind not in {"reply", "delivery"}:
            raise Conflict(
                "Для этой операции доступна остановка без повтора; успешность автоматически не устанавливается"
            )
        if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 100:
            raise ValueError("Укажите ID сообщения Playerok, которое лично проверили")
        account_id = getattr(self.api.account, "id", None)
        if not account_id:
            raise Conflict("Нужна авторизованная сессия Playerok для проверки отправителя")
        text, count = payload.get("text", ""), len(payload.get("photos", []))
        chat_id = target
        if kind == "delivery":
            order = await self.api.call("get_deal", deal_id=target)
            stock = self.store.rows(
                "SELECT i.id,i.secret,i.item_id FROM inventory i JOIN reservations r ON r.inventory_id=i.id WHERE r.order_id=?",
                (target,),
            )
            chat_id = (order.get("chat") or {}).get("id")
            if (
                not stock
                or order.get("id") != target
                or order.get("direction") != "OUT"
                or not chat_id
                or stock[0]["id"] != payload.get("inventory_id")
                or stock[0]["item_id"] != (order.get("item") or {}).get("id")
            ):
                raise Conflict("Резерв не соответствует актуальной продаже")
            text = stock[0]["secret"]
        cursor, seen = None, set()
        for _ in range(20):
            page = await self.api.call("get_chat_messages", chat_id=chat_id, count=24, after_cursor=cursor)
            messages, following = validate_page(page, "messages", seen)
            match = next((message for message in messages if message["id"] == message_id.strip()), None)
            if match:
                if (
                    match.get("deleted_at")
                    or (match.get("user") or {}).get("id") != account_id
                    or not hmac.compare_digest((match.get("text") or "").encode(), text.encode())
                ):
                    raise Conflict("ID найден, но отправитель, текст или доступность не совпадают")
                try:
                    created = datetime.fromisoformat(match["created_at"])
                    queued = datetime.fromisoformat(action["created_at"]).replace(microsecond=0)
                    if created.tzinfo is None or created < queued:
                        raise ValueError
                except (KeyError, TypeError, ValueError):
                    raise Conflict("Сообщение старше операции или время не подтверждено") from None
                images = match.get("images") or []
                if not isinstance(images, list) or len(images) != count:
                    raise Conflict("Количество вложений не совпадает")
                return {
                    "decision": "CONFIRM_MESSAGE",
                    "chat_id": chat_id,
                    "message_id": match["id"],
                    "image_count": count,
                    "image_content_verified": False,
                    "text_matches": True,
                    "sender_matches": True,
                    "checked_at": now(),
                }
            if following is None:
                break
            cursor = following
            seen.add(cursor)
        raise Conflict(
            "Сообщение не найдено в проверенных страницах. Это не доказывает отсутствие отправки; повтор не разрешён"
        )

    async def resolve(self, actor, ident, expected, decision, reason, message_id=None):
        action = self.action(actor, ident, uncertain=True)
        if signature(action) != expected:
            raise Conflict("Операция изменилась. Откройте её заново")
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 500:
            raise ValueError("Комментарий к решению: от 10 до 500 символов; без данных выдачи и секретов")
        if decision == "STOP_UNCERTAIN":
            evidence = {"external_outcome": "UNKNOWN", "checked_at": now()}
            status, error = "PERMANENT_FAILED", "MANUALLY_STOPPED_UNCERTAIN"
        elif decision in {"CONFIRM_MESSAGE", "CONFIRM_READ_STATE"}:
            evidence = await self.evidence(actor, ident, message_id)
            if evidence["decision"] != decision:
                raise Conflict("Тип решения не соответствует операции")
            status, error = "SUCCEEDED", None
        else:
            raise ValueError("Недопустимое решение")
        result = {**evidence, "resolution_source": "OWNER_DECISION", "actor": actor, "decision": decision}
        with self.store.transaction() as db:
            current = db.execute("SELECT * FROM outbox WHERE id=?", (ident,)).fetchone()
            if not current or current["status"] != "UNCERTAIN" or signature(dict(current)) != expected:
                raise Conflict("Операция уже разрешена другим действием")
            if action["kind"] == "delivery" and status == "SUCCEEDED":
                reservation = db.execute(
                    "SELECT i.id,i.status FROM inventory i JOIN reservations r ON r.inventory_id=i.id WHERE r.order_id=?",
                    (action["target"],),
                ).fetchone()
                if (
                    not reservation
                    or reservation["id"] != json.loads(action["payload"]).get("inventory_id")
                    or reservation["status"] != "QUARANTINED"
                ):
                    raise Conflict("Резерв изменился; выдача не может быть подтверждена")
            db.execute(
                "INSERT INTO operation_resolutions VALUES(?,?,?,?,?,?,?,?)",
                (
                    ident,
                    actor,
                    decision,
                    reason.strip(),
                    encode(evidence),
                    current["error_code"],
                    current["result"],
                    now(),
                ),
            )
            db.execute(
                "UPDATE outbox SET status=?,result=?,error_code=?,lease_token=NULL,lease_until=NULL WHERE id=?",
                (status, encode(result), error, ident),
            )
            if action["kind"] == "delivery" and status == "SUCCEEDED":
                db.execute(
                    "UPDATE inventory SET status='DELIVERED' WHERE id=(SELECT inventory_id FROM reservations WHERE order_id=?) AND status='QUARANTINED'",
                    (action["target"],),
                )
            if action["kind"] == "delivery":
                db.execute(
                    "INSERT INTO order_history(order_id,source,new_status,actor,note,created_at) SELECT id,'MANUAL_DELIVERY_RESOLUTION',status,?,?,? FROM orders WHERE id=?",
                    (actor, decision, now(), action["target"]),
                )
            if action["kind"] == "fulfill":
                db.execute(
                    "INSERT INTO order_history(order_id,source,new_status,actor,note,created_at) SELECT id,'MANUAL_FULFILLMENT_RESOLUTION',status,?,?,? FROM orders WHERE id=?",
                    (actor, decision, now(), action["target"]),
                )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'RESOLVE_OPERATION',?,?,?)",
                (actor, ident, decision, now()),
            )
        return result
