import asyncio
import json
import logging
import random
import time

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
        self.wake = asyncio.Event()

    def request(self, kind, target):
        if (
            kind not in {"chat", "deal", "catchup"}
            or not isinstance(target, str)
            or not target
            or len(target) > 100
        ):
            raise ValueError("Некорректный сигнал сверки")
        self.store.execute(
            "INSERT INTO sync_requests(key,kind,target,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET revision=revision+1,updated_at=excluded.updated_at",
            (kind + ":" + target, kind, target, now()),
        )
        self.wake.set()

    async def process_requests(self, limit=8):
        async with self.lock:
            requests = self.store.rows(
                "SELECT * FROM sync_requests WHERE revision>completed_revision AND not_before<=? "
                "ORDER BY updated_at,key LIMIT ?",
                (time.time(), limit),
            )
            for request in requests:
                try:
                    kind, target = request["kind"], request["target"]
                    if kind == "catchup":
                        await self._once()
                        if self.health["status"] == "ERROR":
                            raise APIError("RECONCILE_PARTIAL")
                    elif kind == "deal":
                        deal = await self.api.call("get_deal", deal_id=target)
                        if deal.get("id") != target or deal.get("direction") not in {"IN", "OUT"}:
                            raise APIError("DEAL_ID_OR_DIRECTION_MISMATCH")
                        if deal.get("direction") == "OUT":
                            self.orders.ingest(deal)
                    else:
                        chat = await self.api.call("get_chat", chat_id=target)
                        if chat.get("id") != target:
                            raise APIError("CHAT_ID_MISMATCH")
                        self.ingest_chat(chat)
                        _, complete = await self.scan(
                            "messages:" + target,
                            "get_chat_messages",
                            "messages",
                            lambda message, chat_id=target: self.orders.ingest_message(chat_id, message),
                            chat_id=target,
                        )
                        self.store.execute(
                            "UPDATE sync_chats SET initial_complete=MAX(initial_complete,?),last_synced_at=?,error_code=NULL WHERE id=?",
                            (int(complete), now(), target),
                        )
                except (APIError, ValueError, KeyError, TypeError) as error:
                    failures = request["failures"] + 1
                    code = error.code if isinstance(error, APIError) else "SYNC_REQUEST_SCHEMA_ERROR"
                    delay = min(900, 5 * 2 ** min(failures, 8))
                    retry_after = getattr(error, "retry_after", None)
                    if isinstance(retry_after, (str, int)) and str(retry_after).isdigit():
                        delay = max(delay, int(retry_after))
                    self.store.execute(
                        "UPDATE sync_requests SET failures=?,not_before=?,error_code=? WHERE key=?",
                        (
                            failures,
                            time.time() + delay,
                            code,
                            request["key"],
                        ),
                    )
                else:
                    # Signals arriving during API awaits retain a higher revision.
                    self.store.execute(
                        "UPDATE sync_requests SET completed_revision=MAX(completed_revision,?),failures=0,not_before=0,error_code=NULL WHERE key=?",
                        (request["revision"], request["key"]),
                    )
            pending = self.store.rows(
                "SELECT COUNT(*) AS n FROM sync_requests WHERE revision>completed_revision"
            )[0]["n"]
            self.store.set_setting("realtime:reconciliation", {"pending": pending, "at": now()})
            return len(requests)

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

    def queue_order_details(self, limit=32):
        """Persist a bounded read-only backfill, once per missing-date order.

        Existing request revisions/backoff belong to event reconciliation. Do not
        reset them or requeue an inaccessible/undated historical deal every cycle.
        """
        candidates = self.store.rows(
            "SELECT id FROM orders WHERE (json_extract(payload,'$.created_at') IS NULL "
            "OR json_extract(payload,'$.created_at')='') AND NOT EXISTS "
            "(SELECT 1 FROM sync_requests WHERE key='deal:'||orders.id) ORDER BY id LIMIT ?",
            (limit,),
        )
        for order in candidates:
            self.store.execute(
                "INSERT OR IGNORE INTO sync_requests(key,kind,target,updated_at) VALUES(?,'deal',?,?)",
                ("deal:" + order["id"], order["id"], now()),
            )
        if candidates:
            self.wake.set()
        return len(candidates)

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
        self.queue_order_details()
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
        next_full = 0
        while not stop.is_set():
            self.wake.clear()
            try:
                if time.monotonic() >= next_full:
                    await self.once()
                    failures = failures + 1 if self.health["status"] == "ERROR" else 0
                    next_full = time.monotonic() + min(900, 60 * 2 ** min(failures, 4)) + random.uniform(0, 5)
                processed = await self.process_requests()
                if processed == 8:
                    self.wake.set()
            except APIError as error:
                failures += 1
                self.health = {"status": "ERROR", "code": error.code, "at": now()}
                self.store.set_setting("sync:health", self.health)
                logging.getLogger(__name__).warning("Synchronization failed: %s", error.code)
                next_full = time.monotonic() + min(900, 60 * 2 ** min(failures, 4))
            except Exception as error:  # noqa: BLE001 -- isolate worker failure, persist health and back off
                failures += 1
                self.health = {"status": "ERROR", "code": "SYNC_SCHEMA_OR_STORAGE_ERROR", "at": now()}
                self.store.set_setting("sync:health", self.health)
                logging.getLogger(__name__).error(
                    "Synchronization stopped for this cycle: %s", type(error).__name__
                )
                next_full = time.monotonic() + min(900, 60 * 2 ** min(failures, 4))
            pending = self.store.rows(
                "SELECT MIN(not_before) AS due FROM sync_requests WHERE revision>completed_revision"
            )[0]["due"]
            delay = max(
                1,
                min(
                    60,
                    next_full - time.monotonic(),
                    (max(1, pending - time.time()) if pending is not None else 60),
                ),
            )
            tasks = [asyncio.create_task(stop.wait()), asyncio.create_task(self.wake.wait())]
            try:
                await asyncio.wait(tasks, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
                if not stop.is_set() and self.wake.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=1)
                    except TimeoutError:
                        pass
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
