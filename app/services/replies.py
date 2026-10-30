import uuid

from app.database.store import Conflict, now


def reply_text(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-16-le")) // 2 > 3500:
        raise ValueError("Ответ должен содержать от 1 до 3500 символов")
    return value.strip()


def reply_payload(payload):
    photos = payload.get("photos", [])
    if (
        not isinstance(photos, list)
        or len(photos) > 10
        or any(not isinstance(p, str) or not p for p in photos)
    ):
        raise ValueError("В ответе допустимы до 10 изображений")
    text = payload.get("text", "")
    if photos and isinstance(text, str) and not text.strip():
        return {"text": "", "photos": photos}
    return {"text": reply_text(text), **({"photos": photos} if photos else {})}


class Replies:
    def __init__(self, store):
        self.store = store

    def list(self, owner):
        return self.store.rows(
            "SELECT * FROM quick_replies WHERE owner_id=? ORDER BY created_at,id", (owner,)
        )

    def get(self, owner, ident):
        rows = self.store.rows("SELECT * FROM quick_replies WHERE owner_id=? AND id=?", (owner, ident))
        if not rows:
            raise Conflict("Быстрый ответ удалён или недоступен")
        return rows[0]

    def save(self, owner, title, text, *, ident=None, revision=None):
        if not isinstance(title, str) or not title.strip() or len(title.encode("utf-16-le")) // 2 > 48:
            raise ValueError("Название должно содержать от 1 до 48 символов")
        text = reply_text(text)
        with self.store.transaction() as db:
            if ident is None:
                if (
                    db.execute("SELECT COUNT(*) FROM quick_replies WHERE owner_id=?", (owner,)).fetchone()[0]
                    >= 20
                ):
                    raise Conflict("Можно сохранить не более 20 быстрых ответов")
                ident = uuid.uuid4().hex[:16]
                db.execute(
                    "INSERT INTO quick_replies VALUES(?,?,?,?,0,?)",
                    (ident, owner, title.strip(), text, now()),
                )
            elif (
                db.execute(
                    "UPDATE quick_replies SET title=?,text=?,revision=revision+1 WHERE id=? AND owner_id=? AND revision=?",
                    (title.strip(), text, ident, owner, revision),
                ).rowcount
                != 1
            ):
                raise Conflict("Ответ изменился. Откройте его заново")
        return ident

    def delete(self, owner, ident, revision):
        if (
            self.store.execute(
                "DELETE FROM quick_replies WHERE id=? AND owner_id=? AND revision=?", (ident, owner, revision)
            ).rowcount
            != 1
        ):
            raise Conflict("Ответ изменился. Откройте его заново")

    def origin(self, owner, telegram_message_id, chat_id):
        rows = self.store.rows(
            "SELECT n.event_key FROM notifications n JOIN inbox i ON i.key=n.event_key "
            "JOIN chat_messages m ON i.key='message:'||m.id "
            "WHERE n.owner_id=? AND n.telegram_message_id=? AND m.chat_id=?",
            (owner, telegram_message_id, chat_id),
        )
        return rows[0]["event_key"] if rows else None

    def enqueue(self, owner, key, pending):
        kind, target = pending["kind"], pending["target"]
        payload = pending["payload"]
        if kind == "reply":
            payload = reply_payload(payload)
        origin = pending.get("origin_event")
        with self.store.transaction() as db:
            if origin and (
                kind not in {"reply", "mark_read"}
                or not db.execute(
                    "SELECT 1 FROM notifications n JOIN chat_messages m ON n.event_key='message:'||m.id "
                    "WHERE n.owner_id=? AND n.event_key=? AND m.chat_id=? AND n.telegram_message_id IS NOT NULL",
                    (owner, origin, target),
                ).fetchone()
            ):
                raise Conflict("Связь с уведомлением не подтверждена")
            ident = self.store.enqueue(key, kind, target, payload, db=db, actor=owner)
            if origin:
                db.execute(
                    "INSERT OR IGNORE INTO notification_actions(operation_id,event_key,owner_id) VALUES(?,?,?)",
                    (ident, origin, owner),
                )
        return ident
