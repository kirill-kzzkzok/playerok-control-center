import asyncio
import hashlib
import logging
import random
import time

from app.database.store import Conflict, encode, now
from app.playerok.adapter import APIError
from app.services.notification_preferences import notification_baseline
from app.services.sync_health import orders_sync_failed
from app.workers.pagination import PageScanner
from app.workers.supervisor import worker_cycle
from playerokapi.enums import ItemDealDirections


def request_sync(store, kind, target):
    if (
        kind not in {"chat", "deal", "catchup"}
        or not isinstance(target, str)
        or not target
        or len(target) > 100
    ):
        raise ValueError("Некорректный сигнал сверки")
    store.execute(
        "INSERT INTO sync_requests(key,kind,target,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET revision=revision+1,updated_at=excluded.updated_at",
        (kind + ":" + target, kind, target, now()),
    )


class Synchronizer(PageScanner):
    def __init__(self, store, api, orders, *, pages_per_stream=4, chats_per_cycle=12):
        if pages_per_stream < 1 or chats_per_cycle < 1:
            raise ValueError("Лимиты синхронизации должны быть положительными")
        super().__init__(store, api, pages_per_stream=pages_per_stream)
        self.orders, self.chats_per_cycle = orders, chats_per_cycle
        self.health = {"status": "NOT_STARTED"}
        self.lock = asyncio.Lock()
        self.request_lock = asyncio.Lock()
        self.chat_lock = asyncio.Lock()
        self._background_chats = False
        self._chat_task = None
        self.wake = asyncio.Event()
        notification_baseline(store)

    def request(self, kind, target):
        request_sync(self.store, kind, target)
        self.wake.set()

    async def process_requests(self, limit=8, *, chat_only=False):
        async with self.chat_lock if chat_only else self.request_lock:
            lane = (
                " AND kind='chat' " if chat_only else " AND kind!='chat' " if self._background_chats else " "
            )
            requests = self.store.rows(
                "SELECT * FROM sync_requests WHERE revision>completed_revision AND not_before<=? "
                + lane
                + "ORDER BY CASE WHEN julianday(updated_at)<=julianday('now','-30 seconds') THEN 0 "
                "WHEN kind='deal' THEN 1 ELSE 2 END,updated_at,key LIMIT ?",
                (time.time(), limit),
            )
            for request in requests:
                try:
                    kind, target = request["kind"], request["target"]
                    if kind == "catchup":
                        async with self.lock:
                            await self._once()
                        self._start_chat_refresh()
                        if orders_sync_failed(self.store):
                            raise APIError("RECONCILE_PARTIAL")
                    elif kind == "deal":
                        deal = await self.api.call("get_deal", deal_id=target)
                        if deal.get("id") != target or deal.get("direction") not in {"IN", "OUT"}:
                            raise APIError("DEAL_ID_OR_DIRECTION_MISMATCH")
                        if deal.get("direction") == "OUT":
                            self.orders.ingest(deal)
                    else:
                        if chat_only:
                            await self._requested_chat(target)
                        else:
                            async with self.chat_lock:
                                await self._requested_chat(target)
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

    async def _requested_chat(self, target):
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

    def invalid_order(self, snapshot, error):
        ident = snapshot.get("id") if isinstance(snapshot, dict) else None
        ident = ident if isinstance(ident, str) and ident else None
        conflict = isinstance(error, Conflict)
        evidence = encode(snapshot)
        key = "sync:invalid:" + hashlib.sha256(evidence.encode()).hexdigest()
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) "
                "VALUES(?,'sync_deal_evidence',?,?,?)",
                (key, evidence, now(), now()),
            )
            db.execute(
                "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) "
                "VALUES(?,'enrollment_blocked',?,?,?)",
                (
                    "sync:invalid:notice:" + (ident or "unidentified"),
                    encode({"order_id": ident, "snapshot_wait": not conflict, "identity_conflict": conflict}),
                    now(),
                    now(),
                ),
            )
            if conflict and ident:
                # System-origin annotation; never claim the owner made a decision.
                db.execute(
                    "INSERT INTO order_annotations(order_id,problem,note,revision,actor,updated_at) "
                    "SELECT id,1,?,1,0,? FROM orders WHERE id=? "
                    "ON CONFLICT(order_id) DO UPDATE SET problem=1,note=excluded.note,"
                    "revision=order_annotations.revision+1,actor=0,updated_at=excluded.updated_at "
                    "WHERE order_annotations.problem=0",
                    (
                        "Playerok вернул противоречащую идентичность сделки. Нужна проверка этой сделки.",
                        now(),
                        ident,
                    ),
                )
        if ident:
            self.request("deal", ident)

    def ingest_chat(self, chat):
        self.store.execute(
            "INSERT INTO sync_chats(id,payload,discovered_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (chat["id"], encode(chat), now()),
        )

    async def once(self):
        async with self.lock:
            await self._once()
        if self._background_chats:
            self._start_chat_refresh()
            await asyncio.sleep(0)
        else:
            await self._chat_refresh()

    def _start_chat_refresh(self):
        if self._chat_task is not None and not self._chat_task.done():
            return
        previous, self._chat_task = self._chat_task, None
        if previous is not None:
            try:
                previous.result()
            except asyncio.CancelledError:
                pass
            except Exception as error:  # noqa: BLE001 -- consume owned failure and restart this lane
                logging.getLogger(__name__).error("Chat sync child failed: %s", type(error).__name__)
        self._chat_task = asyncio.create_task(self._chat_refresh(), name="playerok-chat-sync")

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
        orders_retry_after = 0
        streams = [
            ("orders", "get_deals", "deals", self.orders.ingest, {"direction": ItemDealDirections.OUT}),
        ]
        for key, method, collection, ingest, kwargs in streams:
            try:
                counts[key], complete = await self.scan(
                    key,
                    method,
                    collection,
                    ingest,
                    on_entry_error=self.invalid_order if key == "orders" else None,
                    **kwargs,
                )
                if key == "orders" and complete:
                    self.store.set_setting("orders:last_complete_sync", now())
            except APIError as error:
                errors[key] = error.code
                if key == "orders" and str(error.retry_after or "").isdigit():
                    orders_retry_after = int(error.retry_after)
        self.queue_order_details()
        errors = {
            **{
                k: v
                for k, v in self.health.get("errors", {}).items()
                if k == "chats" or k.startswith("messages:")
            },
            **errors,
        }
        self._health(errors, counts, orders_retry_after)

    async def _chat_refresh(self):
        try:
            await self.process_requests(chat_only=True)
            async with self.chat_lock:
                await self._chats_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 -- isolate chat lane, never paid order reads
            errors = {
                k: v
                for k, v in self.health.get("errors", {}).items()
                if not (k == "chats" or k.startswith("messages:"))
            }
            errors["chats"] = (
                error.code if isinstance(error, APIError) else "CHAT_SYNC_SCHEMA_OR_STORAGE_ERROR"
            )
            self._health(errors, {}, self.health.get("orders_retry_after", 0))

    async def _chats_once(self):
        errors = {}
        try:
            await self.scan("chats", "get_chats", "chats", self.ingest_chat)
        except APIError as error:
            errors["chats"] = error.code
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
        errors = {
            **{
                k: v
                for k, v in self.health.get("errors", {}).items()
                if not (k == "chats" or k.startswith("messages:"))
            },
            **errors,
        }
        self._health(errors, {}, self.health.get("orders_retry_after", 0))

    def _health(self, errors, counts, orders_retry_after):
        backlog = self.store.rows("SELECT COUNT(*) AS n FROM sync_chats WHERE initial_complete=0")[0]["n"]
        initialized = bool(
            self.store.setting("orders:completed_at")
            and self.store.setting("chats:completed_at")
            and backlog == 0
        )
        self.health = {
            **self.health,
            "status": "ERROR" if errors else "OK" if initialized else "BACKFILL",
            "at": now(),
            "orders": counts.get("orders", self.health.get("orders", 0)),
            "chat_backlog": backlog,
            "errors": errors,
            "orders_retry_after": orders_retry_after,
        }
        self.store.set_setting("sync:health", self.health)

    async def run(self, stop):
        self._background_chats = True
        try:
            await self._run(stop)
        finally:
            if self._chat_task is not None:
                self._chat_task.cancel()
                await asyncio.gather(self._chat_task, return_exceptions=True)
            self._background_chats = False

    async def _run(self, stop):
        failures = 0
        next_full = 0
        while not stop.is_set():
            self.wake.clear()
            try:
                if time.monotonic() >= next_full:
                    scan_started = time.monotonic()
                    with worker_cycle(self.store, "sync", min(900, 60 * 2 ** min(failures + 1, 4))) as cycle:
                        await self.once()
                        cycle["processed"] = self.health.get("orders", 0)
                        if orders_sync_failed(self.store):
                            cycle["error_code"] = self.health.get("errors", {}).get(
                                "orders", "ORDER_SYNC_FAILED"
                            )
                            if cycle["error_code"] == "CONNECTION_NOT_VERIFIED":
                                cycle["retry_delay"] = 30
                        cycle["retry_delay"] = max(
                            cycle["retry_delay"], self.health.get("orders_retry_after", 0)
                        )
                    failures = failures + 1 if orders_sync_failed(self.store) else 0
                    if failures:
                        next_full = (
                            time.monotonic()
                            + cycle["retry_delay"]
                            + (
                                0
                                if cycle.get("error_code") == "CONNECTION_NOT_VERIFIED"
                                else random.uniform(0, 5)
                            )
                        )
                    else:
                        # Include read time in the healthy cadence. Positive
                        # jitter after30s silently exceeded the detection target.
                        next_full = max(time.monotonic(), scan_started + 30 - random.uniform(0, 5))
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
                "SELECT MIN(not_before) AS due FROM sync_requests WHERE revision>completed_revision AND kind!='chat'"
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
