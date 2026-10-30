"""Enrollment and post-delivery orchestration only; all writes use the shared Outbox."""

import asyncio

from app.database.store import Conflict
from app.gifts.service import GiftService
from app.services.fulfillment import Fulfillment


class GiftWorker:
    def __init__(self, store, api, settings, sender):
        self.service = GiftService(store, api, settings, sender)
        self.store, self.api = store, api

    async def once(self):
        service = self.service
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
            await self.once()
            try:
                await asyncio.wait_for(stop.wait(), 5)
            except TimeoutError:
                pass
