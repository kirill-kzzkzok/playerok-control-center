"""Enrollment and post-delivery orchestration only; all writes use the shared Outbox."""

import asyncio

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

    async def once(self):
        service = self.service
        if service.runtime():
            for row in self.store.rows(
                "SELECT o.id,o.item_id FROM orders o JOIN gift_listings l ON l.item_id=o.item_id WHERE o.status='PAID' AND l.enabled=1 AND json_extract(o.payload,'$.chat.id') IS NULL AND NOT EXISTS(SELECT 1 FROM gift_jobs g WHERE g.order_id=o.id) ORDER BY o.created_at DESC LIMIT 10"
            ):
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
        created = service.discover()
        for j in self.store.rows(
            "SELECT g.* FROM gift_jobs g WHERE "
            "(state='SUCCEEDED' AND (completion_state='WAITING' OR NOT EXISTS "
            "(SELECT 1 FROM outbox o WHERE o.operation_key='gift-notice:'||g.order_id||':DELIVERED'))) "
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
            if not listing["auto_fulfill"]:
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
            if not listing["enabled"] or not service.runtime():
                self.store.execute(
                    "UPDATE gift_jobs SET completion_state='MANUAL' WHERE order_id=?", (j["order_id"],)
                )
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
