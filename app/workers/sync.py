import asyncio
import json
import logging
import random

from app.database.store import encode, now
from app.playerok.adapter import APIError, validate_page
from playerokapi.enums import ItemDealDirections


class Synchronizer:
    def __init__(self, store, api, orders, *, pages_per_stream=4, chats_per_cycle=12):
        if pages_per_stream < 1 or chats_per_cycle < 1:
            raise ValueError("Лимиты синхронизации должны быть положительными")
        self.store, self.api, self.orders = store, api, orders
        self.pages_per_stream, self.chats_per_cycle = pages_per_stream, chats_per_cycle
        self.health = {"status": "NOT_STARTED"}
        self.lock = asyncio.Lock()

    async def scan(self, key, method, collection, ingest, **kwargs):
        """Poll the head, then resume a bounded history pass. Never infer cursor ordering.

        Entries commit before the checkpoint: interruption replays at most a page,
        and durable inbox keys deduplicate it. Completed passes restart from the head
        next cycle, reconciling changes behind opaque/offset cursors as well.
        """
        rows = self.store.rows("SELECT cursor FROM sync_cursors WHERE key=?", (key,))
        checkpoint = json.loads(rows[0]["cursor"]) if rows and rows[0]["cursor"] else {}
        cursor, seen = checkpoint.get("after"), checkpoint.get("seen", [])
        count = 0

        async def fetch(after, visited):
            page = await self.api.call(method, after_cursor=after, **kwargs)
            entries, following = validate_page(page, collection, visited)
            for entry in entries:
                ingest(entry)
            return len(entries), following

        try:
            if cursor is not None:
                head_count, _ = await fetch(None, ())
                count += head_count
            for _ in range(self.pages_per_stream):
                size, following = await fetch(cursor, seen)
                count += size
                if following is not None:
                    seen.append(following)
                    if len(seen) > 10000:
                        raise APIError("PAGINATION_PASS_LIMIT")
                state = {"after": following, "seen": seen if following else []}
                self.store.execute(
                    "INSERT INTO sync_cursors VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                    (key, encode(state), now()),
                )
                if following is None:
                    self.store.set_setting(key + ":completed_at", now())
                    return count, True
                cursor = following
        except APIError as error:
            # A cursor may expire or change meaning. A new pass reconciles from head;
            # already committed records remain durable and are deduplicated.
            if error.code in {
                "INVALID_PAGINATION_CURSOR",
                "PAGINATION_PASS_LIMIT",
                "GRAPHQL_ERROR_OR_INCOMPLETE",
                "HTTP_400",
            }:
                self.store.execute("DELETE FROM sync_cursors WHERE key=?", (key,))
            raise
        return count, False

    def ingest_chat(self, chat):
        self.store.execute(
            "INSERT INTO sync_chats(id,payload,discovered_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (chat["id"], encode(chat), now()),
        )

    async def once(self):
        async with self.lock:
            await self._once()

    async def _once(self):
        errors, counts = {}, {}
        streams = [
            ("orders", "get_deals", "deals", self.orders.ingest, {"direction": ItemDealDirections.OUT}),
            ("chats", "get_chats", "chats", self.ingest_chat, {}),
        ]
        for key, method, collection, ingest, kwargs in streams:
            try:
                counts[key], complete = await self.scan(key, method, collection, ingest, **kwargs)
                if key == "orders" and complete:
                    self.store.set_setting("orders:last_complete_sync", now())
            except APIError as error:
                errors[key] = error.code
        # Oldest attempted chat first. An inaccessible chat cannot starve the others.
        chats = self.store.rows(
            "SELECT id FROM sync_chats ORDER BY last_synced_at IS NOT NULL,last_synced_at,id LIMIT ?",
            (self.chats_per_cycle,),
        )
        for chat in chats:
            ident = chat["id"]
            try:
                _, complete = await self.scan(
                    "messages:" + ident,
                    "get_chat_messages",
                    "messages",
                    lambda message, chat_id=ident: self.orders.ingest_message(chat_id, message),
                    chat_id=ident,
                )
            except APIError as error:
                errors["messages:" + ident] = error.code
                self.store.execute(
                    "UPDATE sync_chats SET last_synced_at=?,error_code=? WHERE id=?",
                    (now(), error.code, ident),
                )
            else:
                self.store.execute(
                    "UPDATE sync_chats SET last_synced_at=?,initial_complete=MAX(initial_complete,?),error_code=NULL WHERE id=?",
                    (now(), int(complete), ident),
                )
        backlog = self.store.rows("SELECT COUNT(*) AS n FROM sync_chats WHERE initial_complete=0")[0]["n"]
        initialized = bool(
            self.store.setting("orders:completed_at")
            and self.store.setting("chats:completed_at")
            and backlog == 0
        )
        self.health = {
            "status": "ERROR" if errors else "OK" if initialized else "BACKFILL",
            "at": now(),
            "orders": counts.get("orders", 0),
            "chat_backlog": backlog,
            "errors": errors,
        }
        self.store.set_setting("sync:health", self.health)

    async def run(self, stop):
        failures = 0
        while not stop.is_set():
            try:
                await self.once()
                failures = failures + 1 if self.health["status"] == "ERROR" else 0
            except APIError as error:
                failures += 1
                self.health = {"status": "ERROR", "code": error.code, "at": now()}
                self.store.set_setting("sync:health", self.health)
                logging.getLogger(__name__).warning("Synchronization failed: %s", error.code)
            except Exception as error:  # noqa: BLE001 -- isolate worker failure, persist health and back off
                failures += 1
                self.health = {"status": "ERROR", "code": "SYNC_SCHEMA_OR_STORAGE_ERROR", "at": now()}
                self.store.set_setting("sync:health", self.health)
                logging.getLogger(__name__).error(
                    "Synchronization stopped for this cycle: %s", type(error).__name__
                )
            delay = min(900, 60 * 2 ** min(failures, 4)) + random.uniform(0, 5)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue
