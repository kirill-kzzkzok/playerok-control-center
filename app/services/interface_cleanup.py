import asyncio
import time

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app.database.store import Conflict, now


class InterfaceCleanup:
    """Explicit bounded Telegram-only deletion. Unknown outcomes are never retried."""

    def __init__(self, store):
        self.store = store

    def owner(self, db, actor):
        if not db.execute("SELECT 1 FROM administrators WHERE user_id=?", (actor,)).fetchone():
            raise Conflict("Доступ закрыт")

    def preview(self, actor, bot_id, current_id):
        at = time.time()
        with self.store.transaction() as db:
            self.owner(db, actor)
            rows = db.execute(
                "SELECT message_id,touched_at FROM panel_messages p WHERE bot_id=? AND owner_id=? "
                "AND status='ACTIVE' AND message_id<>? AND touched_at<=? AND created_at>=? "
                "AND NOT EXISTS(SELECT 1 FROM notifications n WHERE n.owner_id=p.owner_id AND n.telegram_message_id=p.message_id) "
                "AND NOT EXISTS(SELECT 1 FROM chat_cards c WHERE c.owner_id=p.owner_id AND c.telegram_message_id=p.message_id) "
                "AND NOT EXISTS(SELECT 1 FROM operation_receipts r WHERE r.owner_id=p.owner_id AND r.telegram_message_id=p.message_id) "
                "ORDER BY touched_at,message_id LIMIT 20",
                (bot_id, actor, current_id, at - 300, at - 47 * 3600),
            ).fetchall()
            return [dict(row) for row in rows]

    def totals(self, actor, bot_id):
        with self.store.transaction() as db:
            self.owner(db, actor)
            return {
                row["status"]: row["n"]
                for row in db.execute(
                    "SELECT status,count(*) AS n FROM panel_messages WHERE bot_id=? AND owner_id=? GROUP BY status",
                    (bot_id, actor),
                ).fetchall()
            }

    async def execute(self, actor, bot, current_id, snapshot, cleanup_id):
        if not isinstance(snapshot, list) or len(snapshot) > 20 or not cleanup_id:
            raise ValueError("Некорректный план очистки")
        at = time.time()
        with self.store.transaction() as db:
            self.owner(db, actor)
            rows = db.execute(
                "SELECT message_id,touched_at FROM panel_messages p WHERE bot_id=? AND owner_id=? "
                "AND status='ACTIVE' AND message_id<>? AND touched_at<=? AND created_at>=? "
                "AND NOT EXISTS(SELECT 1 FROM notifications n WHERE n.owner_id=p.owner_id AND n.telegram_message_id=p.message_id) "
                "AND NOT EXISTS(SELECT 1 FROM chat_cards c WHERE c.owner_id=p.owner_id AND c.telegram_message_id=p.message_id) "
                "AND NOT EXISTS(SELECT 1 FROM operation_receipts r WHERE r.owner_id=p.owner_id AND r.telegram_message_id=p.message_id)",
                (bot.id, actor, current_id, at - 300, at - 47 * 3600),
            ).fetchall()
            eligible = {row["message_id"]: row["touched_at"] for row in rows}
            if any(
                not isinstance(row, dict)
                or type(row.get("message_id")) is not int
                or row["message_id"] not in eligible
                or eligible.get(row["message_id"]) != row.get("touched_at")
                for row in snapshot
            ):
                raise Conflict("Список сообщений изменился. Откройте превью очистки заново")
            if len({row["message_id"] for row in snapshot}) != len(snapshot):
                raise ValueError("Повторяющиеся сообщения")
            for row in snapshot:
                db.execute(
                    "UPDATE panel_messages SET status='DELETING',cleanup_id=? WHERE bot_id=? AND owner_id=? AND message_id=? AND status='ACTIVE'",
                    (cleanup_id, bot.id, actor, row["message_id"]),
                )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'INTERFACE_CLEANUP',?,?,?)",
                (actor, cleanup_id, "REQUESTED:" + str(len(snapshot)), now()),
            )
        for row in snapshot:
            status, error, stop = "UNCERTAIN", None, False
            try:
                deleted = await bot.delete_message(chat_id=actor, message_id=row["message_id"])
                status = "DELETED" if deleted is True else "UNCERTAIN"
            except asyncio.CancelledError:
                self.finish(bot.id, actor, row["message_id"], cleanup_id, "UNCERTAIN", "CANCELLED")
                raise
            except TelegramRetryAfter:
                status, error, stop = "FAILED", "TELEGRAM_RATE_LIMITED", True
            except TelegramBadRequest:
                status, error = "FAILED", "TELEGRAM_REJECTED"
            except Exception:  # noqa: BLE001 -- deleting may have succeeded; do not retry automatically
                error, stop = "TELEGRAM_RESULT_UNKNOWN", True
            self.finish(bot.id, actor, row["message_id"], cleanup_id, status, error)
            if stop:
                break
        # Unattempted members of a stopped batch stay conservative, without auto-resume.
        self.store.execute(
            "UPDATE panel_messages SET status='UNCERTAIN',error_code='BATCH_INTERRUPTED' WHERE cleanup_id=? AND bot_id=? AND owner_id=? AND status='DELETING'",
            (cleanup_id, bot.id, actor),
        )
        return self.summary(actor, bot.id, cleanup_id)

    def finish(self, bot_id, actor, message_id, cleanup_id, status, error):
        self.store.execute(
            "UPDATE panel_messages SET status=?,error_code=? WHERE bot_id=? AND owner_id=? AND message_id=? AND cleanup_id=? AND status='DELETING'",
            (status, error, bot_id, actor, message_id, cleanup_id),
        )

    def summary(self, actor, bot_id, cleanup_id):
        with self.store.transaction() as db:
            self.owner(db, actor)
            return {
                row["status"]: row["n"]
                for row in db.execute(
                    "SELECT status,count(*) AS n FROM panel_messages WHERE bot_id=? AND owner_id=? AND cleanup_id=? GROUP BY status",
                    (bot_id, actor, cleanup_id),
                ).fetchall()
            }

    def recover(self):
        return self.store.execute(
            "UPDATE panel_messages SET status='UNCERTAIN',error_code='RESTART_DURING_CLEANUP' WHERE status='DELETING'"
        ).rowcount
