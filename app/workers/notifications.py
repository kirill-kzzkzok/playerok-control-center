import asyncio
import json
from html import escape

from app.database.store import encode
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


class Notifications:
    """One attempt per owner/event; uncertain Telegram sends require manual reconciliation."""

    def __init__(self, store, bot):
        self.store, self.bot = store, bot

    async def once(self):
        if not self.store.rows("SELECT user_id FROM administrators LIMIT 1"):
            return
        # First completed sync establishes baseline: do not flood with historical orders.
        if not self.store.setting("notifications:initialized", False):
            if self.store.setting("sync:health", {}).get("status") != "OK":
                return
            with self.store.transaction() as db:
                db.execute(
                    "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) SELECT i.key,a.user_id,'BASELINE' FROM inbox i CROSS JOIN administrators a"
                )
                db.execute(
                    "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("notifications:initialized", encode(True)),
                )
            return
        events = self.store.rows(
            "SELECT i.* FROM inbox i WHERE i.processed_at IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM administrators a WHERE NOT EXISTS "
            "(SELECT 1 FROM notifications n WHERE n.event_key=i.key AND n.owner_id=a.user_id)) "
            "ORDER BY i.received_at,i.key LIMIT 200"
        )
        owners = self.store.rows("SELECT user_id FROM administrators")
        for event in events:
            payload = json.loads(event["payload"])
            if event["kind"] == "message":
                author = payload.get("user") or {}
                account_id = self.store.setting("playerok:account_id")
                if account_id and author.get("id") == account_id:
                    self.store.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) "
                        "SELECT ?,user_id,'SKIPPED' FROM administrators",
                        (event["key"],),
                    )
                    continue
                rows = self.store.rows("SELECT chat_id FROM chat_messages WHERE id=?", (payload["id"],))
                chat_id = rows[0]["chat_id"] if rows else None
                text = "💬 <b>Новое сообщение</b>\n" + escaped(
                    payload.get("text") or "[вложение/событие]", 2800
                )
                markup = (
                    keyboard(
                        [
                            [("✉️ Ответить", "reply:" + chat_id), ("📖 Диалог", "chat:" + chat_id)],
                            [("✅ Прочитано", "readchat:" + chat_id)],
                        ]
                    )
                    if chat_id
                    else keyboard([])
                )
            elif event["kind"] == "order":
                item = payload.get("item") or {}
                text = f"🛒 <b>Заказ</b>\n{escape(item.get('name') or payload['id'])}\nСтатус: {escape(payload['status'])}\nСумма: {item.get('price', '?')} ₽"
                markup = keyboard([[("Открыть заказ", "order:" + payload["id"])]])
            else:
                self.store.execute(
                    "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) "
                    "SELECT ?,user_id,'SKIPPED' FROM administrators",
                    (event["key"],),
                )
                continue
            for owner in owners:
                user = owner["user_id"]
                with self.store.transaction() as db:
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO notifications(event_key,owner_id,status) VALUES(?,?,'PROCESSING')",
                        (event["key"], user),
                    ).rowcount
                if not inserted:
                    continue
                try:
                    message = await self.bot.send_message(user, text, reply_markup=markup)
                except asyncio.CancelledError:
                    self.store.execute(
                        "UPDATE notifications SET status='UNCERTAIN' WHERE event_key=? AND owner_id=?",
                        (event["key"], user),
                    )
                    raise
                except Exception as error:  # noqa: BLE001 -- API may have accepted message; never retry blindly
                    self.store.execute(
                        "UPDATE notifications SET status='UNCERTAIN' WHERE event_key=? AND owner_id=?",
                        (event["key"], user),
                    )
                    self.store.audit(user, "NOTIFICATION", event["key"], type(error).__name__)
                else:
                    self.store.execute(
                        "UPDATE notifications SET status='SUCCEEDED',telegram_message_id=? WHERE event_key=? AND owner_id=?",
                        (message.message_id, event["key"], user),
                    )

    async def run(self, stop):
        self.store.execute("UPDATE notifications SET status='UNCERTAIN' WHERE status='PROCESSING'")
        while not stop.is_set():
            await self.once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                continue
