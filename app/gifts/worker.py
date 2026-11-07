"""Enrollment and post-delivery orchestration only; all writes use the shared Outbox."""

import asyncio

from telethon.errors import RPCError

from app.database.store import Conflict
from app.gifts.service import GiftService
from app.playerok.adapter import APIError
from app.services.fulfillment import Fulfillment
from app.services.order_management import deal_details, deliverable
from app.services.orders import Orders
from app.workers.supervisor import worker_cycle


class GiftWorker:
    def __init__(self, store, api, settings, sender):
        self.service = GiftService(store, api, settings, sender)
        self.store, self.api = store, api

    async def reconcile_due(self):
        """Read one durably ACK-correlated receipt; never retry a paid RPC."""
        import time

        rows = self.store.rows(
            "SELECT o.id,g.order_id,l.owner_id FROM outbox o "
            "JOIN gift_steps s ON s.operation_id=o.id JOIN gift_jobs g ON g.order_id=s.order_id "
            "JOIN gift_listings l ON l.item_id=g.item_id "
            "WHERE o.kind='gift_send' AND o.status='UNCERTAIN' AND o.started_at IS NOT NULL "
            "AND o.retry_at<=? AND g.state='UNCERTAIN' AND g.refund_state='NONE' "
            "AND s.state='UNCERTAIN' AND s.receipt_mode='MESSAGE_ID' AND s.ack_message_id IS NOT NULL "
            "AND NOT EXISTS(SELECT 1 FROM outbox_late_results r WHERE r.operation_id=o.id) "
            "ORDER BY o.retry_at,o.created_at LIMIT 1",
            (time.time(),),
        )
        if not rows:
            return 0
        row = rows[0]
        # Existing retry_at paces READS only. Status never returns to a send queue.
        changed = self.store.execute(
            "UPDATE outbox SET retry_at=? WHERE id=? AND status='UNCERTAIN' AND retry_at<=?",
            (time.time() + 60, row["id"], time.time()),
        ).rowcount
        if not changed:
            return 0
        try:
            async with asyncio.timeout(15):
                await self.service.reconcile(row["owner_id"], row["order_id"])
        except (ValueError, Conflict, APIError, OSError, TimeoutError, RPCError):
            return 0  # No evidence is never permission to retry payment.
        return 1

    async def once(self):
        service = self.service
        verified = await self.reconcile_due()
        recovered = await service.recover_resource_preflights()
        if service.runtime():
            cursor_key = "gifts:details_cursor:" + self.api.account.id
            cursor = self.store.setting(cursor_key, "")
            query = (
                "SELECT o.id,o.item_id FROM orders o JOIN gift_listings l ON l.item_id=o.item_id WHERE o.status='PAID' AND l.enabled=1 "
                "AND (json_extract(o.payload,'$.chat.id') IS NULL OR json_extract(o.payload,'$.has_problem') IS NULL OR json_extract(o.payload,'$.is_automated') IS NULL) "
                "AND NOT EXISTS(SELECT 1 FROM gift_jobs g WHERE g.order_id=o.id) AND o.id>? ORDER BY o.id LIMIT 10"
            )
            rows = self.store.rows(query, (cursor,))
            if not rows and cursor:
                rows = self.store.rows(query, ("",))
            for row in rows:
                try:
                    order = await deal_details(self.api, row["id"])
                    deliverable(order, row["id"], self.api.account.id)
                    if order["item"]["id"] != row["item_id"]:
                        raise Conflict("Предмет сделки изменился")
                    Orders(self.store).ingest(order)
                except (ValueError, APIError) as error:
                    self.store.audit(
                        None, "GIFT_DEAL_DETAILS", row["id"], getattr(error, "code", "DETAILS_NOT_CONFIRMED")
                    )
            # Persist progress across restarts; an incomplete batch must not
            # monopolize every cycle. Repeating after a crash performs reads only.
            self.store.set_setting(cursor_key, rows[-1]["id"] if rows else "")
        created = service.discover() + recovered + verified
        for j in self.store.rows(
            "SELECT g.* FROM gift_jobs g WHERE "
            "(state='SUCCEEDED' AND (completion_state='WAITING' OR NOT EXISTS "
            "(SELECT 1 FROM outbox o WHERE o.operation_key='gift-notice:'||g.order_id||':DELIVERED'))) "
            "AND (completion_state='WAITING' OR NOT EXISTS (SELECT 1 FROM logical_order_scopes s "
            "WHERE s.order_id=g.order_id AND json_type(s.config,'$.gift_notice')='false')) "
            "OR (state='INVALID_RECIPIENT' AND refund_state NOT IN ('QUEUED','SPENDING') AND NOT EXISTS "
            "(SELECT 1 FROM outbox o WHERE o.operation_key='gift-notice:'||g.order_id||':'||"
            "CASE WHEN g.refund_state='CONFIRMED' THEN 'REFUNDED' ELSE 'INVALID' END)) "
            "ORDER BY created_at,order_id LIMIT 50"
        ):
            outcome = (
                "DELIVERED"
                if j["state"] == "SUCCEEDED"
                else "REFUNDED"
                if j["refund_state"] == "CONFIRMED"
                else "INVALID"
            )
            # Do not announce manual handling while a known refund is still in progress.
            if outcome == "INVALID" and j["refund_state"] in {"QUEUED", "SPENDING"}:
                continue
            with self.store.transaction() as db:
                service.queue_notice(db, j, outcome)
            listing = service.listing(j["item_id"])
            if j["state"] != "SUCCEEDED" or j["completion_state"] != "WAITING":
                continue
            if not service.completion_allowed(j, listing):
                self.store.execute(
                    "UPDATE gift_jobs SET completion_state='MANUAL' WHERE order_id=?", (j["order_id"],)
                )
                continue
            if (
                self.store.setting("gifts:paused", False)
                and not self.store.setting("gifts:stopped", True)
                and listing["enabled"]
            ):
                # Temporary admission pause does not revoke post-delivery intent.
                # Resume still runs fresh fulfillment checks and revision CAS.
                continue
            if not listing["enabled"] or self.store.setting("gifts:stopped", True):
                self.store.execute(
                    "UPDATE gift_jobs SET completion_state='MANUAL' WHERE order_id=?", (j["order_id"],)
                )
                continue
            if not service.runtime():
                # Connection/sync recovery must not revoke already approved
                # post-delivery intent. Fresh fulfillment guards run on resume.
                continue
            try:
                prepared = await Fulfillment(self.store, self.api).load(j["order_id"])
                if prepared["mode"] != "legacy":
                    # Provider/report data must not be fabricated. Owner completes the existing form.
                    self.store.execute(
                        "UPDATE gift_jobs SET completion_state='MANUAL' WHERE order_id=?", (j["order_id"],)
                    )
                    continue
                with self.store.transaction() as db:
                    current = service.listing(j["item_id"])
                    if (
                        not service.runtime()
                        or not current["enabled"]
                        or current["revision"] != j["listing_revision"]
                    ):
                        continue
                    op = self.store.enqueue(
                        "gift-fulfill:" + j["order_id"],
                        "fulfill",
                        j["order_id"],
                        {
                            "snapshot": prepared["snapshot"],
                            "mode": "legacy",
                            "report": {},
                            "attested": True,
                            "gift_proof": j["order_id"],
                            "actor": listing["owner_id"],
                        },
                        db,
                        actor=listing["owner_id"],
                    )
                    db.execute(
                        "UPDATE gift_jobs SET completion_operation_id=?,completion_state='QUEUED' WHERE order_id=?",
                        (op, j["order_id"]),
                    )
            except (ValueError, Conflict):
                # Delivery remains confirmed, completion remains available to the owner.
                self.store.execute(
                    "UPDATE gift_jobs SET completion_state='MANUAL' WHERE order_id=?", (j["order_id"],)
                )
                continue
        return created

    async def run(self, stop):
        while not stop.is_set():
            with worker_cycle(self.store, "gifts", 5, max_seconds=180) as cycle:
                cycle["processed"] = await self.once()
            try:
                await asyncio.wait_for(stop.wait(), 5)
            except TimeoutError:
                pass
