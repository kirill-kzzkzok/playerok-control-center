import asyncio
import time

from app.database.store import Conflict, encode, now
from app.playerok.adapter import APIError
from app.services.auto_replies import AutoReplies
from app.services.products import ValidationError
from app.services.sync_health import chat_sync_ready, orders_sync_ready


class AutoReplyWorker:
    def __init__(self, store, api):
        self.store, self.api = store, api
        self.service = AutoReplies(store, api)

    async def once(self):
        account = getattr(self.api.account, "id", None)
        if (
            not account
            or not self.api.account.writes_enabled
            or not getattr(self.api.account, "automation_enabled", False)
        ):
            self.store.set_setting("auto_replies:health", {"status": "DISABLED", "at": now()})
            return 0
        deadline = self.store.setting("auto_replies:not_before", 0)
        if deadline > time.time():
            self.store.set_setting(
                "auto_replies:health", {"status": "BACKOFF", "retry_at": deadline, "at": now()}
            )
            return 0
        rules = self.store.rows(
            "SELECT MIN(updated_at) AS cutoff FROM auto_reply_rules WHERE account_id=? AND enabled=1",
            (account,),
        )
        cutoff = rules[0]["cutoff"]
        if not cutoff:
            self.store.set_setting("auto_replies:health", {"status": "NO_ENABLED_RULES", "at": now()})
            return 0
        health = self.store.setting("sync:health", {})
        complete = health.get("status") == "OK"
        if not complete and not orders_sync_ready(self.store):
            self.store.set_setting("auto_replies:health", {"status": "WAITING_SYNC_BASELINE", "at": now()})
            return 0
        failed_chats = [
            key.split(":", 1)[1] for key in (health.get("errors") or {}) if key.startswith("messages:")
        ]
        candidates = self.store.rows(
            "SELECT m.id,m.chat_id,m.payload,i.received_at FROM chat_messages m JOIN inbox i ON i.key='message:'||m.id "
            "WHERE i.received_at>=? AND NOT EXISTS(SELECT 1 FROM auto_reply_events e WHERE e.account_id=? AND e.message_id=m.id) "
            "AND (? OR m.chat_id IN (SELECT id FROM sync_chats WHERE initial_complete=1 AND error_code IS NULL "
            "AND id NOT IN (SELECT value FROM json_each(?)))) "
            "ORDER BY i.received_at,m.id LIMIT 20",
            (cutoff, account, complete, encode(failed_chats)),
        )
        processed, waiting = 0, 0
        for row in candidates:
            if not chat_sync_ready(self.store, row["chat_id"]):
                waiting += 1
                continue
            processed += 1
            try:
                await self.service.consider(row)
            except APIError as error:
                self.store.execute(
                    "INSERT OR IGNORE INTO auto_reply_events VALUES(?,?,?,NULL,NULL,'BLOCKED',NULL,?,?)",
                    (account, row["id"], row["chat_id"], error.code, now()),
                )
                delay = max(60, int(error.retry_after)) if str(error.retry_after or "").isdigit() else 60
                deadline = time.time() + delay
                self.store.set_setting("auto_replies:not_before", deadline)
                self.store.set_setting(
                    "auto_replies:health",
                    {"status": "ERROR", "code": error.code, "retry_at": deadline, "at": now()},
                )
                return len(candidates)
            except (Conflict, ValidationError, ValueError, KeyError, TypeError):
                self.store.execute(
                    "INSERT OR IGNORE INTO auto_reply_events VALUES(?,?,?,NULL,NULL,'BLOCKED',NULL,'REVIEW_REQUIRED',?)",
                    (account, row["id"], row["chat_id"], now()),
                )
        self.store.set_setting(
            "auto_replies:health",
            {
                "status": "WAITING_SYNC_BASELINE" if waiting and not processed else "ACTIVE",
                "processed": processed,
                "waiting": waiting,
                "at": now(),
            },
        )
        return processed

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.once()
            except Exception as error:  # noqa: BLE001 -- isolate worker, never log message contents
                self.store.set_setting(
                    "auto_replies:health", {"status": "ERROR", "code": type(error).__name__, "at": now()}
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                pass
